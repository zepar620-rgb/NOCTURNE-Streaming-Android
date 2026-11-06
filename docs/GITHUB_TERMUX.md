# Upload NOCTURNE 4.4 to GitHub from Termux

## 1. Create an empty GitHub repository

On GitHub, create a repository (for example `NOCTURNE-Streaming-Android`). Do not initialize it with a README, license, or `.gitignore`, because those files are already included in this project. Copy the HTTPS repository URL.

## 2. Extract and initialize the project in Termux

Download `NOCTURNE-Streaming-Android-4.4.0.zip` to the Android `Download` folder first. Then run:

```bash
pkg update -y && pkg install -y git unzip
termux-setup-storage
mkdir -p ~/projects
unzip -o /sdcard/Download/NOCTURNE-Streaming-Android-4.4.0.zip -d ~/projects
cd ~/projects/NOCTURNE-Streaming-Android

# Set these to your GitHub account details
git config --global user.name "YOUR_GITHUB_USERNAME"
git config --global user.email "YOUR_GITHUB_EMAIL"

git init -b main
git add .
git commit -m "NOCTURNE 4.4 Android project"

# Replace the URL with your own empty GitHub repository URL
git remote add origin https://github.com/YOUR_GITHUB_USERNAME/NOCTURNE-Streaming-Android.git
git push -u origin main
```

If the ZIP was saved under a different name or folder, change the path in the `unzip` command. Replace every `YOUR_GITHUB_...` placeholder before running the commands.

## 3. Authenticate the push

When Git asks for credentials, enter your GitHub username. For the password prompt, use a GitHub Personal Access Token (PAT) with access to that repository and **Contents: Read and write** permission. GitHub account passwords are not accepted for Git over HTTPS. Do not paste the token into the command or commit it into the repository.

## 4. Download the APK from GitHub Actions

After the push succeeds, open the repository on GitHub → **Actions** → **Build NOCTURNE Android APK**. Open the latest successful run and download the artifact named `NOCTURNE-4.4-debug-APK` from the **Artifacts** section. Extract that artifact ZIP to get `app-debug.apk`. You can also start a build manually from **Actions → Build NOCTURNE Android APK → Run workflow**.

## 5. Push later changes

Whenever you edit the project, run these commands from the project directory:

```bash
git add .
git commit -m "Update NOCTURNE"
git push
```

A push to `main` starts the workflow automatically. The artifact contains a **debug APK** for testing; it is not a Play Store release-signed APK.

For an existing checkout, see [`UPDATE_TERMUX.md`](UPDATE_TERMUX.md) for the update/commit/push procedure.
