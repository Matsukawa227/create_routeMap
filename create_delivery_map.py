from __future__ import annotations

from datetime import datetime
import argparse
import html
import logging
import sys
from pathlib import Path

import folium
import pandas as pd
from branca.element import Element

if sys.platform == "win32":
    import msvcrt

MAX_COURSES = 30
COLORS = ["#D32F2F", "#1976D2", "#388E3C", "#F57C00", "#7B1FA2", "#00796B", "#C2185B", "#5D4037", "#455A64", "#827717"]
SHAPES = ["circle", "square", "diamond"]
ALIASES = {
    "latitude": ["緯度", "latitude", "lat"],
    "longitude": ["経度", "longitude", "lng", "lon", "long"],
    "course_code": ["コースコード", "コース", "コースcd", "course_code", "coursecode", "course"],
    "delivery_order": ["配達順", "配送順", "訪問順", "順番", "delivery_order", "deliveryorder", "order", "sequence"],
}


def read_csv(path: Path) -> tuple[pd.DataFrame, str]:
    last_error = None
    for encoding in ("utf-8-sig", "utf-8", "cp932"):
        try:
            return pd.read_csv(path, encoding=encoding, dtype=str, keep_default_na=False), encoding
        except UnicodeDecodeError as error:
            last_error = error
    raise ValueError("CSVの文字コードを判定できません。UTF-8またはShift-JISで保存してください。") from last_error


def normalize(value: object) -> str:
    text = str(value).strip().lower()
    for char in (" ", "　", "_", "-", "－", "(", ")", "（", "）"):
        text = text.replace(char, "")
    return text


def standardize_columns(df: pd.DataFrame) -> pd.DataFrame:
    normalized = {normalize(column): column for column in df.columns}
    rename = {}
    missing = []
    labels = {"latitude": "緯度", "longitude": "経度", "course_code": "コースコード", "delivery_order": "配達順"}
    for standard, aliases in ALIASES.items():
        actual = next((normalized.get(normalize(alias)) for alias in aliases if normalized.get(normalize(alias)) is not None), None)
        if actual is None:
            missing.append(labels[standard])
        else:
            rename[actual] = standard
    if missing:
        raise ValueError("必要な列が見つかりません: " + ", ".join(missing))
    extra = [str(column) for column in df.columns if column not in rename and str(column).strip()]
    if extra:
        raise ValueError("CSVに使用できない列があります: " + ", ".join(extra) + "\n使用できる列は緯度、経度、コースコード、配達順のみです。")
    return df.rename(columns=rename).copy()


def prepare_data(df: pd.DataFrame) -> tuple[pd.DataFrame, list[str], list[str], int]:
    if df.empty:
        raise ValueError("CSVにデータ行がありません。")
    df = standardize_columns(df)
    df["_csv_row"] = range(2, len(df) + 2)
    for column in ("latitude", "longitude", "course_code", "delivery_order"):
        df[column] = df[column].astype(str).str.strip()

    blank_mask = df["latitude"].eq("") | df["longitude"].eq("")
    skipped = df[blank_mask].copy()
    skip_details = []
    for _, row in skipped.iterrows():
        missing = []
        if not row["latitude"]:
            missing.append("緯度")
        if not row["longitude"]:
            missing.append("経度")
        skip_details.append(
            f"スキップ: CSV {int(row['_csv_row'])}行目 / コース={row['course_code'] or '未入力'}"
            f" / 配達順={row['delivery_order'] or '未入力'} / 空白項目={','.join(missing)}"
        )
    skipped_count = len(skipped)
    warnings = []
    if skipped_count:
        warnings.append(f"緯度または経度が空白のため、{skipped_count}件をスキップしました。")

    df = df[~blank_mask].copy()
    if df.empty:
        raise ValueError(f"地図に表示できる地点がありません。座標空白のため{skipped_count}件すべてをスキップしました。")
    for column in ("latitude", "longitude", "delivery_order"):
        df[column] = pd.to_numeric(df[column], errors="coerce")

    errors = []
    for _, row in df.iterrows():
        line = int(row["_csv_row"])
        lat, lng, order = row["latitude"], row["longitude"], row["delivery_order"]
        if pd.isna(lat):
            errors.append(f"{line}行目: 緯度が数値ではありません。")
        elif not -90 <= float(lat) <= 90:
            errors.append(f"{line}行目: 緯度は-90から90の範囲で指定してください。")
        if pd.isna(lng):
            errors.append(f"{line}行目: 経度が数値ではありません。")
        elif not -180 <= float(lng) <= 180:
            errors.append(f"{line}行目: 経度は-180から180の範囲で指定してください。")
        if not row["course_code"]:
            errors.append(f"{line}行目: コースコードが空です。")
        if pd.isna(order):
            errors.append(f"{line}行目: 配達順が数値ではありません。")
        elif float(order) < 1 or not float(order).is_integer():
            errors.append(f"{line}行目: 配達順は1以上の整数にしてください。")
    if errors:
        message = "\n".join(errors[:20])
        if len(errors) > 20:
            message += f"\nほか{len(errors) - 20}件のエラーがあります。"
        raise ValueError("CSVの内容にエラーがあります。\n" + message)

    df["delivery_order"] = df["delivery_order"].astype(int)
    courses = sorted(df["course_code"].unique(), key=str)
    if len(courses) > MAX_COURSES:
        raise ValueError(f"コース数が{len(courses)}件あります。最大{MAX_COURSES}コースまでです。")
    duplicates = df[df.duplicated(["course_code", "delivery_order"], keep=False)]
    if not duplicates.empty:
        pairs = duplicates[["course_code", "delivery_order"]].drop_duplicates().values.tolist()
        warnings.append("同一コース内で配達順が重複しています: " + ", ".join(f"{c}の配達順{o}" for c, o in pairs[:10]))
    same_coordinates = df[df.duplicated(["latitude", "longitude"], keep=False)]
    if not same_coordinates.empty:
        count = len(same_coordinates[["latitude", "longitude"]].drop_duplicates())
        warnings.append(f"同一座標が{count}か所あります。ピンが重なって見える可能性があります。")
    return df.sort_values(["course_code", "delivery_order", "_csv_row"], kind="stable").reset_index(drop=True), warnings, skip_details, skipped_count


def marker_html(color: str, shape: str, order: int) -> str:
    size = 12 if order < 100 else 10 if order < 1000 else 8
    return f"<div class='pin pin-{shape}' style='background:{color};font-size:{size}px'><span>{order}</span></div>"


def popup_html(row: pd.Series) -> str:
    course = html.escape(str(row["course_code"]))
    return (
        "<div class='popup'><b>配達地点</b><table>"
        f"<tr><th>コース</th><td>{course}</td></tr>"
        f"<tr><th>配達順</th><td>{int(row['delivery_order'])}</td></tr>"
        f"<tr><th>緯度</th><td>{float(row['latitude']):.7f}</td></tr>"
        f"<tr><th>経度</th><td>{float(row['longitude']):.7f}</td></tr>"
        "</table></div>"
    )


def create_map(df: pd.DataFrame, output: Path) -> int:
    route_map = folium.Map(location=[df["latitude"].mean(), df["longitude"].mean()], zoom_start=11, tiles="OpenStreetMap", control_scale=True, prefer_canvas=True)
    css = """<style>
.pin{width:34px;height:34px;display:flex;align-items:center;justify-content:center;box-sizing:border-box;border:3px solid white;color:white;font-family:Segoe UI,Meiryo,sans-serif;font-weight:700;box-shadow:0 2px 5px #555}.pin-circle{border-radius:50%}.pin-square{border-radius:4px}.pin-diamond{border-radius:4px;transform:rotate(45deg)}.pin-diamond span{transform:rotate(-45deg)}.popup{min-width:210px;font-family:Segoe UI,Meiryo,sans-serif}.popup table{width:100%;border-collapse:collapse}.popup th,.popup td{padding:3px 5px;border-bottom:1px solid #eee;text-align:left}.leaflet-control-layers{max-height:55vh;overflow:auto}
</style>"""
    route_map.get_root().header.add_child(Element(css))
    all_points = []
    courses = sorted(df["course_code"].unique(), key=str)
    for index, course in enumerate(courses):
        color, shape = COLORS[index % 10], SHAPES[index // 10]
        course_df = df[df["course_code"] == course].sort_values(["delivery_order", "_csv_row"])
        group = folium.FeatureGroup(name=f"{course} ({len(course_df)}件)", show=True)
        points = []
        for _, row in course_df.iterrows():
            point = [float(row["latitude"]), float(row["longitude"])]
            points.append(point); all_points.append(point)
            order = int(row["delivery_order"])
            folium.Marker(
                point,
                tooltip=f"コース: {html.escape(str(course))} / 配達順: {order}",
                popup=folium.Popup(popup_html(row), max_width=380),
                icon=folium.DivIcon(html=marker_html(color, shape, order), icon_size=(34, 34), icon_anchor=(17, 17), popup_anchor=(0, -17), class_name="delivery-div-icon"),
            ).add_to(group)
        if len(points) >= 2:
            folium.PolyLine(points, color=color, weight=4, opacity=.8, tooltip=f"コース: {html.escape(str(course))}").add_to(group)
        group.add_to(route_map)
    folium.LayerControl(position="topright", collapsed=False).add_to(route_map)
    if len(all_points) == 1:
        route_map.location = all_points[0]; route_map.options["zoom"] = 16
    else:
        route_map.fit_bounds(all_points, padding=(35, 35))
    route_map.save(str(output))
    return len(courses)


def output_paths(csv_path: Path, timestamp: str, specified: str | None) -> tuple[Path, Path]:
    html_path = Path(specified).expanduser() if specified else csv_path.parent / f"{csv_path.stem}_{timestamp}.html"
    log_path = csv_path.parent / f"{csv_path.stem}_{timestamp}.log"
    return html_path, log_path


def setup_logger(path: Path) -> logging.Logger:
    logger = logging.getLogger("route_map")
    logger.setLevel(logging.INFO); logger.propagate = False
    for handler in logger.handlers[:]:
        handler.close(); logger.removeHandler(handler)
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")
    console = logging.StreamHandler(sys.stdout); console.setFormatter(formatter)
    file_handler = logging.FileHandler(path, mode="w", encoding="utf-8-sig"); file_handler.setFormatter(formatter)
    logger.addHandler(console); logger.addHandler(file_handler)
    return logger


def close_logger(logger: logging.Logger | None) -> None:
    if logger:
        for handler in logger.handlers[:]:
            handler.flush(); handler.close(); logger.removeHandler(handler)


def wait_for_key() -> None:
    print("\n任意のキーを押すと終了します。")
    try:
        msvcrt.getwch() if sys.platform == "win32" else input()
    except (EOFError, KeyboardInterrupt):
        pass


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="配達コース地図作成")
    parser.add_argument("csv_file", nargs="?", default="delivery.csv", help="入力CSV。EXEへのドラッグ＆ドロップ可")
    parser.add_argument("output_html", nargs="?", default=None, help="任意の出力HTML")
    return parser.parse_args()


def main() -> int:
    args = parse_arguments()
    csv_path = Path(args.csv_file).expanduser()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path, log_path = output_paths(csv_path, timestamp, args.output_html)
    logger = None
    try:
        if not csv_path.parent.exists():
            print(f"エラー: CSVの保存先フォルダーがありません。\n対象: {csv_path}"); return 1
        logger = setup_logger(log_path)
        logger.info("配達コース地図作成 開始")
        logger.info("入力CSV: %s", csv_path.resolve())
        logger.info("出力HTML: %s", output_path.resolve())
        logger.info("実行ログ: %s", log_path.resolve())
        if not csv_path.exists():
            logger.error("CSVファイルが見つかりません。"); return 1
        if not csv_path.is_file() or csv_path.suffix.lower() != ".csv":
            logger.error("CSVファイルを指定してください。"); return 1
        if output_path.suffix.lower() not in {".html", ".htm"}:
            logger.error("出力拡張子は.htmlまたは.htmにしてください。"); return 1
        source, encoding = read_csv(csv_path)
        source_count = len(source)
        df, warnings, skip_details, skipped_count = prepare_data(source)
        course_count = create_map(df, output_path)
        logger.info("文字コード: %s", encoding)
        logger.info("CSVデータ件数: %d件", source_count)
        logger.info("地図表示件数: %d件", len(df))
        logger.info("座標空白スキップ件数: %d件", skipped_count)
        logger.info("コース数: %d件", course_count)
        for message in warnings:
            logger.warning(message)
        for detail in skip_details:
            logger.warning(detail)
        logger.info("地図の作成が完了しました。")
        return 0
    except PermissionError as error:
        if logger: logger.exception("ファイルにアクセスできません: %s", error)
        else: print(f"ファイルにアクセスできません: {error}")
        return 1
    except (ValueError, pd.errors.ParserError) as error:
        if logger: logger.error("処理を中止しました。\n%s", error)
        else: print(f"エラー: {error}")
        return 1
    except Exception as error:
        if logger: logger.exception("予期しないエラー: %s: %s", type(error).__name__, error)
        else: print(f"予期しないエラー: {type(error).__name__}: {error}")
        return 1
    finally:
        if logger: logger.info("処理を終了します。")
        close_logger(logger)


if __name__ == "__main__":
    exit_code = 1
    try:
        exit_code = main()
    finally:
        wait_for_key()
    raise SystemExit(exit_code)
