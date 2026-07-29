#!/usr/bin/env bash
# exit on error
set -o errexit

# 1. ติดตั้ง Python Library ตามปกติ
pip install -r requirements.txt

# 2. ดาวน์โหลด FFmpeg Static Binary มาไว้ในโฟลเดอร์โปรเจกต์บน Render
if [ ! -d "ffmpeg" ]; then
  echo "📥 กำลังดาวน์โหลด FFmpeg..."
  mkdir -p ffmpeg
  cd ffmpeg
  wget https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz
  tar -xf ffmpeg-release-amd64-static.tar.xz --strip-components=1
  cd ..
  echo "✅ ติดตั้ง FFmpeg สำเร็จ!"
fi