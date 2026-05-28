#!/bin/bash
set -e
cd "$(dirname "$0")"

# gcloud PATH 등록
for p in \
  "/opt/homebrew/Caskroom/google-cloud-sdk/latest/google-cloud-sdk/bin" \
  "/usr/local/Caskroom/google-cloud-sdk/latest/google-cloud-sdk/bin" \
  "/usr/local/google-cloud-sdk/bin" \
  "/opt/google-cloud-sdk/bin" \
  "$HOME/google-cloud-sdk/bin"; do
  [ -d "$p" ] && export PATH="$p:$PATH" && break
done

echo "📦 의존성 설치 중..."
pip3 install -r requirements.txt -q

echo ""
echo "🚀 GCP Auditor 시작: http://0.0.0.0:9090"
echo "   종료: Ctrl+C"
echo ""
python3 -m uvicorn main:app --host 0.0.0.0 --port 9090
