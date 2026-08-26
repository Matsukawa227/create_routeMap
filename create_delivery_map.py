"""
配達コース地図作成ツール

CSVから以下の情報を読み込み、FoliumでHTML地図を生成します。

必須項目:
    緯度
    経度
    コースコード
    配達順

主な機能:
    ・最大30コース
    ・10色 × 3形状でコースを識別
    ・ピン内に配達順を表示
    ・配達順にルート線を描画
    ・コース別に表示、非表示
    ・全地点が収まるように自動調整
    ・日本語、英語の列名に対応
    ・UTF-8、Shift-JIS系CSVに対応
    ・座標、配達順、重複などをチェック

実行例:
    python create_delivery_map.py delivery.csv

出力先を指定する場合:
    python create_delivery_map.py delivery.csv delivery_map.html
"""

from __future__ import annotations
from datetime import datetime

import argparse
import html
import math
import sys
from pathlib import Path

import folium
import pandas as pd
from branca.element import Element


# ============================================================
# 基本設定
# ============================================================

MAX_COURSES = 30

# 10色 × 3形状 = 30パターン
COURSE_COLORS = [
    "#D32F2F",  # 赤
    "#1976D2",  # 青
    "#388E3C",  # 緑
    "#F57C00",  # オレンジ
    "#7B1FA2",  # 紫
    "#00796B",  # 青緑
    "#C2185B",  # ピンク
    "#5D4037",  # 茶
    "#455A64",  # 青灰
    "#827717",  # オリーブ
]

COURSE_SHAPES = [
    "circle",
    "square",
    "diamond",
]

SHAPE_NAMES = {
    "circle": "丸",
    "square": "四角",
    "diamond": "ひし形",
}

# 入力CSVの列名候補
COLUMN_ALIASES = {
    "latitude": [
        "緯度",
        "latitude",
        "lat",
    ],
    "longitude": [
        "経度",
        "longitude",
        "lng",
        "lon",
        "long",
    ],
    "course_code": [
        "コースコード",
        "コース",
        "コースcd",
        "course_code",
        "coursecode",
        "course",
    ],
    "delivery_order": [
        "配達順",
        "配送順",
        "訪問順",
        "順番",
        "delivery_order",
        "deliveryorder",
        "order",
        "sequence",
    ],
}

# 任意列
OPTIONAL_COLUMN_ALIASES = {
    "location_name": [
        "地点名",
        "配達先名",
        "顧客名",
        "名称",
        "name",
        "location_name",
    ],
    "address": [
        "住所",
        "address",
    ],
}


# ============================================================
# CSV読み込み
# ============================================================

def read_csv_with_encoding(csv_path: Path) -> tuple[pd.DataFrame, str]:
    """
    UTF-8 BOM付き、UTF-8、CP932の順にCSVを読み込む。
    日本語Windowsで作成したCSVにも対応する。
    """

    encodings = [
        "utf-8-sig",
        "utf-8",
        "cp932",
    ]

    last_error: Exception | None = None

    for encoding in encodings:
        try:
            dataframe = pd.read_csv(
                csv_path,
                encoding=encoding,
                dtype=str,
                keep_default_na=False,
            )

            return dataframe, encoding

        except UnicodeDecodeError as error:
            last_error = error

    raise ValueError(
        "CSVの文字コードを判定できませんでした。"
        "UTF-8またはShift-JIS形式で保存してください。"
    ) from last_error


def normalize_column_name(value: object) -> str:
    """
    CSV列名を比較しやすい形式に変換する。

    例:
        "Course Code" -> "coursecode"
        "コース コード" -> "コースコード"
    """

    normalized = str(value).strip().lower()

    remove_characters = [
        " ",
        "　",
        "_",
        "-",
        "－",
        "(",
        ")",
        "（",
        "）",
    ]

    for character in remove_characters:
        normalized = normalized.replace(character, "")

    return normalized


def find_column(
    dataframe: pd.DataFrame,
    aliases: list[str],
) -> str | None:
    """
    候補名から実際のCSV列名を探す。
    """

    normalized_columns = {
        normalize_column_name(column): column
        for column in dataframe.columns
    }

    for alias in aliases:
        normalized_alias = normalize_column_name(alias)

        if normalized_alias in normalized_columns:
            return normalized_columns[normalized_alias]

    return None


def standardize_columns(dataframe: pd.DataFrame) -> pd.DataFrame:
    """
    日本語、英語などの列名を内部標準名へ変換する。
    """

    rename_map: dict[str, str] = {}
    missing_columns: list[str] = []

    required_names = {
        "latitude": "緯度",
        "longitude": "経度",
        "course_code": "コースコード",
        "delivery_order": "配達順",
    }

    for standard_name, aliases in COLUMN_ALIASES.items():
        actual_column = find_column(dataframe, aliases)

        if actual_column is None:
            missing_columns.append(required_names[standard_name])
        else:
            rename_map[actual_column] = standard_name

    if missing_columns:
        raise ValueError(
            "必要な列が見つかりません: "
            + ", ".join(missing_columns)
            + "\nCSVの列名を確認してください。"
        )

    for standard_name, aliases in OPTIONAL_COLUMN_ALIASES.items():
        actual_column = find_column(dataframe, aliases)

        if actual_column is not None:
            rename_map[actual_column] = standard_name

    return dataframe.rename(columns=rename_map).copy()


# ============================================================
# データ検証
# ============================================================

def validate_and_prepare_data(
    dataframe: pd.DataFrame,
) -> tuple[pd.DataFrame, list[str]]:
    """
    CSVの値を検証し、描画用データに変換する。

    戻り値:
        検証済みDataFrame
        警告メッセージ一覧
    """

    warnings: list[str] = []

    if dataframe.empty:
        raise ValueError("CSVにデータ行がありません。")

    dataframe = standardize_columns(dataframe)

    # CSV上の行番号。ヘッダーが1行目なのでデータは2行目から。
    dataframe["_csv_row"] = range(2, len(dataframe) + 2)

    # 文字列を整形
    dataframe["course_code"] = (
        dataframe["course_code"]
        .astype(str)
        .str.strip()
    )

    # 数値へ変換できない値はNaN
    dataframe["latitude"] = pd.to_numeric(
        dataframe["latitude"],
        errors="coerce",
    )

    dataframe["longitude"] = pd.to_numeric(
        dataframe["longitude"],
        errors="coerce",
    )

    dataframe["delivery_order"] = pd.to_numeric(
        dataframe["delivery_order"],
        errors="coerce",
    )

    errors: list[str] = []

    for _, row in dataframe.iterrows():
        csv_row = int(row["_csv_row"])

        latitude = row["latitude"]
        longitude = row["longitude"]
        course_code = row["course_code"]
        delivery_order = row["delivery_order"]

        if pd.isna(latitude):
            errors.append(
                f"{csv_row}行目: 緯度が数値ではありません。"
            )
        elif not -90 <= float(latitude) <= 90:
            errors.append(
                f"{csv_row}行目: 緯度は-90から90の範囲で指定してください。"
            )

        if pd.isna(longitude):
            errors.append(
                f"{csv_row}行目: 経度が数値ではありません。"
            )
        elif not -180 <= float(longitude) <= 180:
            errors.append(
                f"{csv_row}行目: 経度は-180から180の範囲で指定してください。"
            )

        if not course_code:
            errors.append(
                f"{csv_row}行目: コースコードが空です。"
            )

        if pd.isna(delivery_order):
            errors.append(
                f"{csv_row}行目: 配達順が数値ではありません。"
            )
        elif float(delivery_order) < 1:
            errors.append(
                f"{csv_row}行目: 配達順は1以上にしてください。"
            )
        elif not float(delivery_order).is_integer():
            errors.append(
                f"{csv_row}行目: 配達順は整数にしてください。"
            )

    if errors:
        displayed_errors = errors[:20]

        message = "\n".join(displayed_errors)

        if len(errors) > 20:
            message += f"\nほか {len(errors) - 20} 件のエラーがあります。"

        raise ValueError(
            "CSVの内容にエラーがあります。\n" + message
        )

    dataframe["delivery_order"] = (
        dataframe["delivery_order"].astype(int)
    )

    course_codes = sorted(
        dataframe["course_code"].unique().tolist(),
        key=lambda value: str(value),
    )

    if len(course_codes) > MAX_COURSES:
        raise ValueError(
            f"コース数が{len(course_codes)}件あります。"
            f"最大{MAX_COURSES}コースまで対応しています。"
        )

    # 同一コース内の配達順重複を確認
    duplicated_orders = dataframe[
        dataframe.duplicated(
            subset=["course_code", "delivery_order"],
            keep=False,
        )
    ].sort_values(
        ["course_code", "delivery_order"]
    )

    if not duplicated_orders.empty:
        duplicate_pairs = (
            duplicated_orders[
                ["course_code", "delivery_order"]
            ]
            .drop_duplicates()
            .values
            .tolist()
        )

        display_pairs = [
            f"{course}の配達順{order_no}"
            for course, order_no in duplicate_pairs[:10]
        ]

        warning = (
            "同一コース内で配達順が重複しています: "
            + ", ".join(display_pairs)
        )

        if len(duplicate_pairs) > 10:
            warning += (
                f"、ほか{len(duplicate_pairs) - 10}件"
            )

        warnings.append(warning)

    # 同一座標の重複を確認
    duplicated_coordinates = dataframe[
        dataframe.duplicated(
            subset=["latitude", "longitude"],
            keep=False,
        )
    ]

    if not duplicated_coordinates.empty:
        coordinate_count = len(
            duplicated_coordinates[
                ["latitude", "longitude"]
            ].drop_duplicates()
        )

        warnings.append(
            f"同一座標が{coordinate_count}か所あります。"
            "ピンが重なって見える可能性があります。"
        )

    # 描画順を統一
    dataframe = dataframe.sort_values(
        [
            "course_code",
            "delivery_order",
            "_csv_row",
        ],
        kind="stable",
    ).reset_index(drop=True)

    return dataframe, warnings


# ============================================================
# マーカー作成
# ============================================================

def get_course_style(course_index: int) -> dict[str, str]:
    """
    コース番号から色と形を決定する。

    0～9:
        10色の丸

    10～19:
        10色の四角

    20～29:
        10色のひし形
    """

    color_index = course_index % len(COURSE_COLORS)
    shape_index = course_index // len(COURSE_COLORS)

    return {
        "color": COURSE_COLORS[color_index],
        "shape": COURSE_SHAPES[shape_index],
    }


def create_marker_html(
    color: str,
    shape: str,
    order_number: int,
) -> str:
    """
    配達順を表示するカスタムマーカーHTMLを作成する。
    """

    # 同一サイズでも、配達順の桁数に応じて少し文字を小さくする。
    if order_number < 100:
        font_size = 12
    elif order_number < 1000:
        font_size = 10
    else:
        font_size = 8

    marker_class = f"delivery-pin delivery-pin-{shape}"

    return f"""
    <div
        class="{marker_class}"
        style="
            background-color: {color};
            font-size: {font_size}px;
        "
    >
        <span class="delivery-pin-text">
            {order_number}
        </span>
    </div>
    """


def create_popup_html(
    row: pd.Series,
) -> str:
    """
    ピンクリック時のポップアップHTMLを作成する。
    """

    course_code = html.escape(
        str(row["course_code"])
    )

    latitude = float(row["latitude"])
    longitude = float(row["longitude"])
    delivery_order = int(row["delivery_order"])

    optional_rows = ""

    if "location_name" in row.index:
        location_name = str(row["location_name"]).strip()

        if location_name:
            optional_rows += f"""
            <tr>
                <th>地点名</th>
                <td>{html.escape(location_name)}</td>
            </tr>
            """

    if "address" in row.index:
        address = str(row["address"]).strip()

        if address:
            optional_rows += f"""
            <tr>
                <th>住所</th>
                <td>{html.escape(address)}</td>
            </tr>
            """

    return f"""
    <div class="delivery-popup">
        <div class="delivery-popup-title">
            配達地点
        </div>

        <table>
            <tr>
                <th>コース</th>
                <td>{course_code}</td>
            </tr>
            <tr>
                <th>配達順</th>
                <td>{delivery_order}</td>
            </tr>
            {optional_rows}
            <tr>
                <th>緯度</th>
                <td>{latitude:.7f}</td>
            </tr>
            <tr>
                <th>経度</th>
                <td>{longitude:.7f}</td>
            </tr>
        </table>
    </div>
    """


# ============================================================
# 凡例
# ============================================================

def build_legend_html(
    course_information: list[dict[str, object]],
    point_count: int,
) -> str:
    """
    左下に表示するコース凡例HTMLを作成する。
    """

    legend_rows: list[str] = []

    for course in course_information:
        course_code = html.escape(
            str(course["course_code"])
        )

        color = str(course["color"])
        shape = str(course["shape"])
        shape_name = SHAPE_NAMES[shape]
        count = int(course["count"])

        legend_rows.append(
            f"""
            <div class="course-legend-row">
                <div
                    class="
                        course-legend-symbol
                        course-legend-{shape}
                    "
                    style="background-color: {color};"
                    title="{shape_name}"
                ></div>

                <div class="course-legend-code">
                    {course_code}
                </div>

                <div class="course-legend-count">
                    {count}件
                </div>
            </div>
            """
        )

    legend_body = "\n".join(legend_rows)

    return f"""
    <div id="course-legend">
        <div id="course-legend-header">
            <span>配達コース</span>
            <button
                id="course-legend-toggle"
                type="button"
                aria-label="凡例を折りたたむ"
            >
                −
            </button>
        </div>

        <div id="course-legend-summary">
            {len(course_information)}コース /
            {point_count}地点
        </div>

        <div id="course-legend-body">
            {legend_body}
        </div>
    </div>

    <script>
        document.addEventListener("DOMContentLoaded", function () {{
            const button =
                document.getElementById("course-legend-toggle");

            const body =
                document.getElementById("course-legend-body");

            const summary =
                document.getElementById("course-legend-summary");

            button.addEventListener("click", function () {{
                const isHidden =
                    body.style.display === "none";

                body.style.display =
                    isHidden ? "block" : "none";

                summary.style.display =
                    isHidden ? "block" : "none";

                button.textContent =
                    isHidden ? "−" : "＋";

                button.setAttribute(
                    "aria-label",
                    isHidden
                        ? "凡例を折りたたむ"
                        : "凡例を展開する"
                );
            }});
        }});
    </script>
    """


# ============================================================
# 地図作成
# ============================================================

def create_delivery_map(
    dataframe: pd.DataFrame,
    output_path: Path,
) -> list[dict[str, object]]:
    """
    配達地図を生成してHTMLとして保存する。
    """

    center_latitude = float(
        dataframe["latitude"].mean()
    )

    center_longitude = float(
        dataframe["longitude"].mean()
    )

    delivery_map = folium.Map(
        location=[
            center_latitude,
            center_longitude,
        ],
        zoom_start=11,
        tiles="OpenStreetMap",
        control_scale=True,
        prefer_canvas=True,
    )

    # マーカーと凡例用CSS
    map_css = """
    <style>
        .delivery-pin {
            width: 34px;
            height: 34px;
            box-sizing: border-box;

            display: flex;
            align-items: center;
            justify-content: center;

            border: 3px solid #ffffff;
            color: #ffffff;

            font-family:
                -apple-system,
                BlinkMacSystemFont,
                "Segoe UI",
                "Yu Gothic UI",
                Meiryo,
                sans-serif;

            font-weight: 700;
            line-height: 1;

            box-shadow:
                0 2px 5px rgba(0, 0, 0, 0.5),
                0 0 0 1px rgba(0, 0, 0, 0.45);

            cursor: pointer;
        }

        .delivery-pin-circle {
            border-radius: 50%;
        }

        .delivery-pin-square {
            border-radius: 4px;
        }

        .delivery-pin-diamond {
            border-radius: 4px;
            transform: rotate(45deg);
        }

        .delivery-pin-diamond .delivery-pin-text {
            transform: rotate(-45deg);
        }

        .delivery-popup {
            min-width: 220px;
            font-family:
                -apple-system,
                BlinkMacSystemFont,
                "Segoe UI",
                "Yu Gothic UI",
                Meiryo,
                sans-serif;
        }

        .delivery-popup-title {
            margin-bottom: 7px;
            font-size: 15px;
            font-weight: 700;
        }

        .delivery-popup table {
            width: 100%;
            border-collapse: collapse;
        }

        .delivery-popup th,
        .delivery-popup td {
            padding: 3px 5px;
            border-bottom: 1px solid #eeeeee;
            text-align: left;
            vertical-align: top;
        }

        .delivery-popup th {
            width: 65px;
            color: #555555;
            white-space: nowrap;
        }

        #course-legend {
            position: fixed;
            left: 20px;
            bottom: 25px;
            z-index: 9999;

            width: 245px;
            max-height: 55vh;

            overflow-y: auto;
            box-sizing: border-box;

            padding: 10px;

            background: rgba(255, 255, 255, 0.96);
            border: 1px solid #888888;
            border-radius: 6px;

            box-shadow: 0 2px 8px rgba(0, 0, 0, 0.28);

            font-family:
                -apple-system,
                BlinkMacSystemFont,
                "Segoe UI",
                "Yu Gothic UI",
                Meiryo,
                sans-serif;
        }

        #course-legend-header {
            display: flex;
            align-items: center;
            justify-content: space-between;

            margin-bottom: 5px;

            font-size: 15px;
            font-weight: 700;
        }

        #course-legend-toggle {
            width: 28px;
            height: 25px;
            padding: 0;

            border: 1px solid #bbbbbb;
            border-radius: 4px;
            background: #ffffff;

            cursor: pointer;
            font-size: 17px;
            line-height: 20px;
        }

        #course-legend-summary {
            margin-bottom: 7px;
            color: #555555;
            font-size: 12px;
        }

        .course-legend-row {
            display: flex;
            align-items: center;
            gap: 8px;

            padding: 4px 2px;
            border-top: 1px solid #eeeeee;
        }

        .course-legend-symbol {
            width: 18px;
            height: 18px;
            min-width: 18px;

            box-sizing: border-box;
            border: 2px solid #ffffff;

            box-shadow:
                0 1px 2px rgba(0, 0, 0, 0.4),
                0 0 0 1px rgba(0, 0, 0, 0.35);
        }

        .course-legend-circle {
            border-radius: 50%;
        }

        .course-legend-square {
            border-radius: 3px;
        }

        .course-legend-diamond {
            border-radius: 2px;
            transform: rotate(45deg) scale(0.82);
        }

        .course-legend-code {
            flex: 1;
            overflow-wrap: anywhere;
            font-size: 13px;
        }

        .course-legend-count {
            color: #666666;
            font-size: 11px;
            white-space: nowrap;
        }

        .leaflet-control-layers {
            max-height: 55vh;
            overflow-y: auto;
        }
    </style>
    """

    delivery_map.get_root().header.add_child(
        Element(map_css)
    )

    course_codes = sorted(
        dataframe["course_code"].unique().tolist(),
        key=lambda value: str(value),
    )

    all_coordinates: list[list[float]] = []
    course_information: list[dict[str, object]] = []

    for course_index, course_code in enumerate(course_codes):
        course_dataframe = dataframe[
            dataframe["course_code"] == course_code
        ].copy()

        course_dataframe = course_dataframe.sort_values(
            ["delivery_order", "_csv_row"],
            kind="stable",
        )

        style = get_course_style(course_index)
        color = style["color"]
        shape = style["shape"]

        # コース単位の表示、非表示を行うレイヤー
        feature_group = folium.FeatureGroup(
            name=(
                f"{course_code} "
                f"({len(course_dataframe)}件)"
            ),
            show=True,
            overlay=True,
            control=True,
        )

        route_coordinates: list[list[float]] = []

        for _, row in course_dataframe.iterrows():
            latitude = float(row["latitude"])
            longitude = float(row["longitude"])
            delivery_order = int(row["delivery_order"])

            coordinate = [
                latitude,
                longitude,
            ]

            route_coordinates.append(coordinate)
            all_coordinates.append(coordinate)

            marker_html = create_marker_html(
                color=color,
                shape=shape,
                order_number=delivery_order,
            )

            popup_html = create_popup_html(row)

            tooltip_parts = [
                f"コース: {html.escape(str(course_code))}",
                f"配達順: {delivery_order}",
            ]

            if "location_name" in row.index:
                location_name = str(
                    row["location_name"]
                ).strip()

                if location_name:
                    tooltip_parts.append(
                        html.escape(location_name)
                    )

            tooltip_text = " / ".join(tooltip_parts)

            # DivIconは標準アイコン画像を使わず、
            # HTMLとCSSでピンを描画する。
            marker = folium.Marker(
                location=coordinate,
                tooltip=folium.Tooltip(
                    tooltip_text,
                    sticky=True,
                ),
                popup=folium.Popup(
                    popup_html,
                    max_width=380,
                ),
                icon=folium.DivIcon(
                    html=marker_html,
                    icon_size=(34, 34),
                    icon_anchor=(17, 17),
                    popup_anchor=(0, -17),
                    class_name="delivery-div-icon",
                ),
            )

            marker.add_to(feature_group)

        # 2地点以上ある場合だけルート線を描画
        if len(route_coordinates) >= 2:
            folium.PolyLine(
                locations=route_coordinates,
                color=color,
                weight=4,
                opacity=0.80,
                tooltip=folium.Tooltip(
                    f"コース: {html.escape(str(course_code))}",
                    sticky=True,
                ),
            ).add_to(feature_group)

        feature_group.add_to(delivery_map)

        course_information.append(
            {
                "course_code": course_code,
                "color": color,
                "shape": shape,
                "count": len(course_dataframe),
            }
        )

    # 右上のコース選択
    folium.LayerControl(
        position="topright",
        collapsed=False,
    ).add_to(delivery_map)

    # 左下の色、形状凡例
    legend_html = build_legend_html(
        course_information=course_information,
        point_count=len(dataframe),
    )

    delivery_map.get_root().html.add_child(
        Element(legend_html)
    )

    # 全地点が表示範囲に収まるようにする
    if len(all_coordinates) == 1:
        delivery_map.location = all_coordinates[0]
        delivery_map.options["zoom"] = 16
    else:
        delivery_map.fit_bounds(
            all_coordinates,
            padding=(35, 35),
        )

    delivery_map.save(str(output_path))

    return course_information


# ============================================================
# コマンドライン
# ============================================================
def get_default_output_filename():
    """
    出力ファイル名に日時を付与する

    例:
    delivery_map_20260826_221530.html
    """

    timestamp = datetime.now().strftime(
        "%Y%m%d_%H%M%S"
    )

    return f"delivery_map_{timestamp}.html"

def parse_arguments():
    parser = argparse.ArgumentParser(
        description="配達コース地図作成"
    )

    parser.add_argument(
        "csv_file",
        nargs="?",
        default="delivery.csv",
        help="入力CSV"
    )

    parser.add_argument(
        "output_html",
        nargs="?",
        default=get_default_output_filename(),
        help="出力HTML"
    )

    return parser.parse_args()


def main() -> int:
    """
    メイン処理。
    """

    arguments = parse_arguments()

    csv_path = Path(arguments.csv_file)
    output_path = Path(arguments.output_html)

    print("=" * 60)
    print("配達コース地図作成")
    print("=" * 60)

    if not csv_path.exists():
        print(
            f"エラー: CSVファイルが見つかりません。\n"
            f"対象: {csv_path.resolve()}",
            file=sys.stderr,
        )
        return 1

    if not csv_path.is_file():
        print(
            f"エラー: 指定されたパスはファイルではありません。\n"
            f"対象: {csv_path.resolve()}",
            file=sys.stderr,
        )
        return 1

    if output_path.suffix.lower() not in {
        ".html",
        ".htm",
    }:
        print(
            "エラー: 出力ファイルの拡張子は"
            ".htmlまたは.htmにしてください。",
            file=sys.stderr,
        )
        return 1

    try:
        dataframe, encoding = read_csv_with_encoding(
            csv_path
        )

        dataframe, warnings = validate_and_prepare_data(
            dataframe
        )

        course_information = create_delivery_map(
            dataframe=dataframe,
            output_path=output_path,
        )

    except PermissionError as error:
        print(
            "エラー: ファイルにアクセスできません。\n"
            "CSVまたは出力HTMLをExcelやブラウザーで"
            "開いたままにしていないか確認してください。\n"
            f"詳細: {error}",
            file=sys.stderr,
        )
        return 1

    except (ValueError, pd.errors.ParserError) as error:
        print(
            f"エラー:\n{error}",
            file=sys.stderr,
        )
        return 1

    except Exception as error:
        print(
            "予期しないエラーが発生しました。\n"
            f"詳細: {type(error).__name__}: {error}",
            file=sys.stderr,
        )
        return 1

    print(f"入力CSV       : {csv_path.resolve()}")
    print(f"文字コード    : {encoding}")
    print(f"地点数        : {len(dataframe)}")
    print(f"コース数      : {len(course_information)}")
    print(f"出力HTML      : {output_path.resolve()}")

    if warnings:
        print("-" * 60)
        print("警告")

        for warning in warnings:
            print(f"・{warning}")

    print("-" * 60)
    print("地図の作成が完了しました。")
    print("出力されたHTMLをブラウザーで開いてください。")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())