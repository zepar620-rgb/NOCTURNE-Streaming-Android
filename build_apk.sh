#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v gradle >/dev/null 2>&1; then
  echo "Gradle tidak ditemukan. Buka folder ini di Android Studio atau install Gradle 8.13."
  exit 1
fi

if [[ -z "${ANDROID_HOME:-}" && -z "${ANDROID_SDK_ROOT:-}" ]]; then
  echo "ANDROID_HOME/ANDROID_SDK_ROOT belum diatur. Install Android SDK dan atur variabel SDK terlebih dahulu."
  exit 1
fi

gradle --no-daemon --stacktrace assembleDebug
APK="app/build/outputs/apk/debug/app-debug.apk"
if [[ -f "$APK" ]]; then
  printf "\nAPK berhasil dibuat: %s\n" "$APK"
else
  echo "Build selesai, tetapi file APK tidak ditemukan di lokasi yang diharapkan."
  exit 1
fi
