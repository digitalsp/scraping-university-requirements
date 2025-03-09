import difflib
import hashlib
import json
import os
import re
import time
from datetime import datetime
from io import BytesIO

import pandas as pd
import requests
from bs4 import BeautifulSoup
from PIL import Image
from selenium import webdriver
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service

# ========== メインの処理 ==========

MIN_IMAGE_WIDTH = 50
MIN_IMAGE_HEIGHT = 50
MAX_RETRIES = 5


def sanitize_filename(name: str) -> str:
    # Windows等で使えない文字をアンダースコアに置き換え
    return re.sub(r'[\\/:*?"<>|]', "_", name)


def get_page_content_with_selenium(url: str, driver: webdriver.Chrome) -> (str, str):
    """
    Seleniumで指定URLのページ内容を取得する。
    最大 MAX_RETRIES 回再試行して、それでも失敗なら (None, エラー文字列) を返す。
    """
    last_exception = None
    for attempt in range(MAX_RETRIES):
        try:
            driver.get(url)
            time.sleep(3.0)  # ページ描画待ち時間 (サイトに応じて調整可能)
            return driver.page_source, None
        except WebDriverException as e:
            last_exception = e
            time.sleep(2)
    return None, str(last_exception)


def compute_image_hashes(soup: BeautifulSoup, base_url: str) -> dict:
    """
    HTML中にある img タグの src 属性を参照し、
    画像のMD5ハッシュを取得して dict に格納して返す。
    ただし、画像サイズが MIN_IMAGE_WIDTH, MIN_IMAGE_HEIGHT 未満のものは対象外。
    """
    images = soup.find_all("img")
    img_hashes = {}
    session = requests.Session()

    for img in images:
        src = img.get("src")
        if not src:
            continue

        # 必要に応じて相対パスを絶対URLに変換するなど補足処理を入れる

        try:
            resp = session.get(src, timeout=10)
            if resp.status_code == 200:
                try:
                    with Image.open(BytesIO(resp.content)) as im:
                        w, h = im.size
                        if w < MIN_IMAGE_WIDTH or h < MIN_IMAGE_HEIGHT:
                            continue  # 小さい画像は無視
                    hash_md5 = hashlib.md5(resp.content).hexdigest()
                    img_hashes[src] = hash_md5
                except Exception as e_img:
                    img_hashes[src] = f"ErrorImage:{str(e_img)}"
            else:
                img_hashes[src] = f"ErrorStatus:{resp.status_code}"
        except Exception as e_req:
            img_hashes[src] = f"Error:{str(e_req)}"

    return img_hashes


def remove_unnecessary_tags(soup: BeautifulSoup):
    """
    差分比較用のテキストを取得しやすくするため、script/style など不要なタグを削除。
    必要に応じて<header>, <footer>, <nav>, <aside>なども除去できる。
    """
    for style_tag in soup.find_all("style"):
        style_tag.decompose()
    for script_tag in soup.find_all("script"):
        script_tag.decompose()
    return soup


def highlight_html_diff(old_html: str, new_html: str) -> str:
    """
    人間が文字として認識できる部分だけを抽出して差分をとり、
    実際の行に対して色を付けてハイライトする。
    """
    old_lines = old_html.splitlines(keepends=True)
    new_lines = new_html.splitlines(keepends=True)

    def extract_readable_text(line: str) -> str:
        # タグを除去
        import re

        tmp = re.sub(r"<.*?>", "", line)
        # 連続する空白をまとめる
        tmp = re.sub(r"\s+", " ", tmp)
        # 前後の空白を削除
        tmp = tmp.strip()
        return tmp

    old_pairs = [(orig, extract_readable_text(orig)) for orig in old_lines]
    new_pairs = [(orig, extract_readable_text(orig)) for orig in new_lines]

    old_text_only = [p[1] for p in old_pairs]
    new_text_only = [p[1] for p in new_pairs]

    sm = difflib.SequenceMatcher(None, old_text_only, new_text_only)
    result = []

    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "equal":
            # この区間は変更なし
            for idx in range(j1, j2):
                result.append(new_pairs[idx][0])
        elif op == "insert":
            # 新しく挿入された行
            for idx in range(j1, j2):
                escaped_line = (
                    new_pairs[idx][0].replace("<", "&lt;").replace(">", "&gt;")
                )
                result.append(
                    f'<span style="background-color: yellow;">{escaped_line}</span>'
                )
        elif op == "delete":
            # 削除された行
            for idx in range(i1, i2):
                escaped_line = (
                    old_pairs[idx][0].replace("<", "&lt;").replace(">", "&gt;")
                )
                result.append(
                    f'<del style="background-color: #fcc;">{escaped_line}</del>'
                )
        elif op == "replace":
            # 変更された行
            for idx in range(j1, j2):
                escaped_line = (
                    new_pairs[idx][0].replace("<", "&lt;").replace(">", "&gt;")
                )
                result.append(
                    f'<span style="background-color: #faa;">{escaped_line}</span>'
                )

    diff_result = "<meta charset='UTF-8'>\n" + "".join(result)
    return diff_result


def main():
    """
    タブ補完による入力補助付きで設定を指定し、CSVをもとに差分取得を行うメイン関数。
    """

    # CSVファイルパス(タブ補完対応)
    default_csv_path = "./test.csv"
    csv_in = input(
        f"CSVファイルのパスを入力してください (既定: {default_csv_path}) : "
    ).strip()
    if csv_in == "":
        csv_in = default_csv_path
    CSV_PATH = csv_in

    # バックアップ用フォルダ
    default_data_folder = "./backuped_datas"
    data_in = input(
        f"バックアップデータ用フォルダ名を入力してください (既定: {default_data_folder}) : "
    ).strip()
    if data_in == "":
        data_in = default_data_folder
    DATA_FOLDER = os.path.join(os.getcwd(), data_in)
    if not os.path.exists(DATA_FOLDER):
        os.makedirs(DATA_FOLDER, exist_ok=True)

    # 差分HTML出力用フォルダ
    default_diff_html_folder = "./diff_html"
    diff_in = input(
        f"差分HTMLを保存するフォルダ名を入力してください (既定: {default_diff_html_folder}) : "
    ).strip()
    if diff_in == "":
        diff_in = default_diff_html_folder
    DIFF_HTML_FOLDER = os.path.join(os.getcwd(), diff_in)
    if not os.path.exists(DIFF_HTML_FOLDER):
        os.makedirs(DIFF_HTML_FOLDER, exist_ok=True)

    # ChromeDriverパス(タブ補完対応)
    default_driver_path = "/usr/bin/chromedriver"
    driver_in = input(
        f"ChromeDriverのパスを入力してください (既定: {default_driver_path}) : "
    ).strip()
    if driver_in == "":
        driver_in = default_driver_path
    DRIVER_PATH = driver_in

    # CSVファイルの存在チェック
    if not os.path.exists(CSV_PATH):
        print(f"[ERROR] 指定されたCSVファイルが見つかりません: {CSV_PATH}")
        return

    # Seleniumのヘッドレス起動
    chrome_options = Options()
    chrome_options.add_argument("--headless")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    service = Service(DRIVER_PATH)
    driver = webdriver.Chrome(service=service, options=chrome_options)

    # CSVを読み込む
    df = pd.read_csv(CSV_PATH, header=None)
    for _, row in df.iterrows():
        name = str(row[0]).strip()
        url = str(row[1]).strip()

        # URLチェック
        if not re.match(r"^https?://", url):
            print(f"[SKIP] 不正なURL: {name} - {url}")
            continue

        file_name = sanitize_filename(name) + ".json"
        file_path = os.path.join(DATA_FOLDER, file_name)

        old_data = None
        if os.path.exists(file_path):
            with open(file_path, "r", encoding="utf-8") as f:
                old_data = json.load(f)

        page_content, error_info = get_page_content_with_selenium(url, driver)
        if page_content is None:
            print(f"[ERROR] ページ取得失敗: {name} - {url}")
            continue

        # 画像ハッシュ計算
        soup = BeautifulSoup(page_content, "html.parser")
        image_hashes = compute_image_hashes(soup, url)

        # テキスト差分用
        soup_for_diff = remove_unnecessary_tags(
            BeautifulSoup(page_content, "html.parser")
        )
        text_for_diff = soup_for_diff.get_text(separator="\n")

        current_time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        new_data = {
            "name": name,
            "url": url,
            "retrieved_at": current_time_str,
            "html_source": page_content,  # HTMLフルソース
            "text_for_diff": text_for_diff,
            "image_hashes": image_hashes,
        }

        # データを保存し、変更があれば差分HTMLを出力
        if old_data is not None:
            old_text = old_data.get("text_for_diff", "")
            if old_text != text_for_diff:
                print(f"[INFO] テキスト変更あり: {name}")
                old_html = old_data.get("html_source", "")
                new_html = new_data.get("html_source", "")

                diff_html = highlight_html_diff(old_html, new_html)
                current_dt = datetime.now()
                date_yyyymmdd = current_dt.strftime("%Y-%m-%d")
                time_hhmm = current_dt.strftime("%H%M")
                diff_filename = (
                    f"diff-{date_yyyymmdd}-{time_hhmm}-{sanitize_filename(name)}.html"
                )
                diff_file_path = os.path.join(DIFF_HTML_FOLDER, diff_filename)

                diff_html_full = f"""
                <html>
                <head>
                <meta charset="UTF-8">
                <title>Diff for {name}</title>
                </head>
                <body>
                {diff_html}
                </body>
                </html>
                """

                with open(diff_file_path, "w", encoding="utf-8") as difff:
                    difff.write(diff_html_full)
                print(f"[DIFF] 差分HTMLを作成: {diff_file_path}")
            else:
                print(f"[INFO] 変更なし: {name}")
        else:
            print(f"[INFO] 初回取得: {name}")

        # 新しいデータをJSONに保存
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(new_data, f, ensure_ascii=False, indent=2)

    driver.quit()
    print("=== 処理終了 ===")


if __name__ == "__main__":
    main()
