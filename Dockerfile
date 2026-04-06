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

# 7. アプリケーションを実行するコマンド
# main.py を python で直接実行する場合の例です。
# Flask や Gunicorn を使う場合は、コマンドを書き換える必要があります。
CMD ["python", "main.py"]
