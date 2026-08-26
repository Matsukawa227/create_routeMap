import pandas as pd
import random

OUTPUT_FILE = "delivery.csv"

# 東京駅付近を中心
CENTER_LAT = 35.681236
CENTER_LNG = 139.767125

COURSE_COUNT = 10
POINTS_PER_COURSE = 50

records = []

for course_no in range(1, COURSE_COUNT + 1):

    course_code = f"C{course_no:03d}"

    # コースごとに開始位置をずらす
    base_lat = CENTER_LAT + (course_no * 0.01)
    base_lng = CENTER_LNG + (course_no * 0.01)

    for order_no in range(1, POINTS_PER_COURSE + 1):

        # 前地点から少しずつ移動するイメージ
        lat = (
            base_lat
            + (order_no * 0.0015)
            + random.uniform(-0.0003, 0.0003)
        )

        lng = (
            base_lng
            + (order_no * 0.0012)
            + random.uniform(-0.0003, 0.0003)
        )

        records.append({
            "緯度": round(lat, 7),
            "経度": round(lng, 7),
            "コースコード": course_code,
            "配達順": order_no,
            "地点名": f"{course_code}_配送先_{order_no:03d}",
            "住所": f"東京都サンプル区{order_no}番地"
        })

df = pd.DataFrame(records)

df.to_csv(
    OUTPUT_FILE,
    index=False,
    encoding="utf-8-sig"
)

print(
    f"{OUTPUT_FILE} を作成しました"
)
print(
    f"コース数: {COURSE_COUNT}"
)
print(
    f"地点数 : {len(df)}"
)