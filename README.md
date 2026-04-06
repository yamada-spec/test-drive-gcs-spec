# test-drive-gcs-spec

Google Drive 上の指定フォルダから **PDF のみ** を読み取り、Google Cloud Storage（GCS）へアップロードするサービスです。**Google Cloud Run** 上で Flask + gunicorn として動作し、HTTP リクエストをトリガに同期を実行します。

## プロジェクトの目的

- 教材・テスト関連の Drive フォルダ構成を、GCS 側の固定パス（`common/textbook/`、`common/test/`）に揃えて置きたい。
- バッチや手動コピーではなく、**デプロイ済みのサービスにリクエストを送るだけで同期**できるようにする。
- **フォルダ ID とバケット名はコードに埋め込まず**、環境変数で渡し、環境ごとの取り違えや誤同期を防ぐ。

## 同期の仕様（現在の実装）

対象は **親フォルダ**（環境変数 `DRIVE_PARENT_FOLDER_ID` で指定）の **直下** にある名前付きサブフォルダのみです。

| Drive（親の直下のフォルダ名） | GCS のプレフィックス |
|------------------------------|----------------------|
| `テキスト`                   | `common/textbook/`   |
| `テスト`                     | `common/test/`       |

- 各サブフォルダ **配下は再帰的** に走査します。
- **MIME が `application/pdf` のファイルのみ** アップロードします（バイナリをそのまま GCS へ保存）。
- Google ドキュメント等の PDF 変換は行いません（Drive 上で PDF として保存されているものが対象）。

## HTTP エンドポイント

| パス | 説明 |
|------|------|
| `GET` / `POST` `/` | 同期処理を実行（環境変数が揃っていることが前提） |
| `GET` `/health` | 生存確認のみ（同期は実行しない。起動・ヘルスチェック用） |

ローカルでは `PORT`（未設定時は `8080`）で `python main.py` が Flask の開発サーバを起動します。本番コンテナでは **gunicorn** が `main:app` を起動します。

## 必要な環境変数

| 変数名 | 必須 | 説明 |
|--------|------|------|
| `GCS_BUCKET` | はい | アップロード先 GCS バケット名 |
| `DRIVE_PARENT_FOLDER_ID` | はい | 同期のルートとなる Drive フォルダ ID（例: 「6年テキストテスト」相当のフォルダ） |

任意・その他:

- `GOOGLE_APPLICATION_CREDENTIALS` … サービスアカウント JSON のパス（ローカルや VM でファイルを使う場合）。
- `GOOGLE_CLOUD_PROJECT` … GCS クライアント用（未設定でも多くの場合は動作します）。
- `PORT` … Cloud Run が注入（通常 `8080`）。gunicorn のバインドに使用。

## 認証・権限（共有時の確認事項）

1. **サービスアカウント**に、対象 Drive フォルダが **共有されている**こと（閲覧可能であること）。共有されていないと Drive API でファイルを取得できません。
2. **GCS** では、同一サービスアカウントに対象バケットへの **オブジェクトの作成・更新** 権限（例: `roles/storage.objectAdmin` など、運用方針に応じて最小権限）を付与する。
3. **Drive API** と **Cloud Storage** を使うため、上記に相当するスコープ・IAM がコード側の想定と一致していることを確認する。

機密情報（フォルダ ID・バケット名・キー）は **リポジトリにコミットせず**、Secret Manager、Cloud Run の環境変数/シークレット、または CI のシークレットで管理する。

## ローカルでの動き方

```bash
pip install -r requirements.txt
set GCS_BUCKET=your-bucket
set DRIVE_PARENT_FOLDER_ID=your-folder-id
python main.py --sync
```

同期のみ CLI で実行する例です（Flask は起動しません）。

開発用に HTTP サーバだけ起動する場合:

```bash
python main.py
# http://127.0.0.1:8080/ で同期、/health で確認
```

## Docker / Cloud Run

- `Dockerfile` で `requirements.txt` をインストールし、`gunicorn` が `0.0.0.0:${PORT:-8080}` にバインドします。
- Cloud Run のサービス設定で **`GCS_BUCKET`** と **`DRIVE_PARENT_FOLDER_ID`** を必ず設定してください。
- 同期が長時間になる場合は、**リクエストタイムアウト**や **同時実行数** を業務に合わせて調整してください。

## これまでの主な変更点（履歴メモ）

- **Cloud Run 起動失敗の修正**: 当初、コンテナ起動時に CLI 用の `main()` が先に走り、環境変数不足で `exit(1)` したり、8080 で待ち受ける前に終了したりしていた。Flask アプリを `main:app` として **gunicorn で起動**し、**`/health`** を追加してプローブと役割を分離した。
- **仕様の絞り込み**: 汎用の複数ルールから、**「テキスト」→ `common/textbook/`、「テスト」→ `common/test/`** の **PDF のみ** 同期に変更。
- **セキュリティ・運用**: Drive のルートフォルダ ID を **コードに固定せず**、**`DRIVE_PARENT_FOLDER_ID` を必須の環境変数**に変更（デプロイ先で明示設定）。
- **依存関係**: PDF のみ扱うため **`html2text` を依存から削除**。

## 共有・引き継ぎ時のチェックリスト

- [ ] リポジトリに **バケット名・フォルダ ID・鍵ファイル** が含まれていないこと。
- [ ] 本番・検証で **`GCS_BUCKET` / `DRIVE_PARENT_FOLDER_ID`** が意図した値か。
- [ ] サービスアカウントに **Drive の共有** と **GCS 書き込み** が付いているか。
- [ ] Cloud Run の **タイムアウト**、**メモリ**、**同時実行** が同期規模に足りるか。
- [ ] 同期の起動方法（**誰が `/` にリクエストするか**、認証・IP 制限・IAM 等）が組織ポリシーに合うか。

## ライセンス・問い合わせ

（必要に応じて組織のライセンス方針・連絡先を追記してください。）
