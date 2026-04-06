
# 1. ベースイメージの指定 (Python 3.11のスリム版を使用)
FROM python:3.11-slim

# 2. コンテナ内の作業ディレクトリを設定
WORKDIR /app

# 3. 依存ライブラリの定義ファイルをコピー (もしあれば)
# requirements.txt がない場合は、この行と次の行をコメントアウトしてください
COPY requirements.txt .

# 4. 依存ライブラリをインストール
# requirements.txt がない場合は、コメントアウトしてください
RUN pip install --no-cache-dir -r requirements.txt

# 5. 現在のディレクトリのコードをコンテナ内にコピー
COPY . .

# 6. Cloud Run がリクエストを受け付けるポート (デフォルトは8080)
EXPOSE 8080

# 7. Cloud Run は環境変数 PORT（通常 8080）で待ち受ける。gunicorn で Flask アプリを起動する。
# main.py 先頭で main() を実行しないこと（以前はここで exit(1) になりプローブ失敗していた）
CMD ["sh", "-c", "exec gunicorn --bind 0.0.0.0:${PORT:-8080} --workers 1 --threads 8 --timeout 0 main:app"]
