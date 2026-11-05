# Update NOCTURNE 4.2 from Termux

The source ZIP intentionally excludes `.git`, so applying it will not overwrite the GitHub remote, branch history, or local credentials in your existing checkout.

## 1. Install Termux tools and back up local data

```bash
pkg update -y
pkg install -y git unzip
termux-setup-storage
mkdir -p ~/projects
```

Back up `nocturne.db` first if you run the Flask project directly from Termux. For the installed APK, export **Tools & Backup → Ekspor JSON** before replacing the app.

## 2. Prepare your repository checkout

If you already cloned the project, open that folder. Otherwise, clone it once:

```bash
git clone https://github.com/zepar620-rgb/NOCTURNE-Streaming-Android.git ~/projects/NOCTURNE-Streaming-Android
```

Copy `NOCTURNE-Streaming-Android-4.2.0.zip` into Android's Download folder, then overlay the updated files onto the checkout:

```bash
cd ~/projects/NOCTURNE-Streaming-Android
git pull --rebase origin main
unzip -o /sdcard/Download/NOCTURNE-Streaming-Android-4.2.0.zip -d ~/projects
git status
```

The ZIP has a `NOCTURNE-Streaming-Android/` top-level folder, so extraction into `~/projects` updates the matching checkout without touching `.git`. If your checkout lives elsewhere, replace `~/projects` with its parent folder.

## 3. Commit and push the update

```bash
cd ~/projects/NOCTURNE-Streaming-Android
git add -A
git commit -m "NOCTURNE 4.2: Pinterest wallpapers, stream resolutions and anime quotes"
git push origin main
```

Authenticate with your GitHub username and a Personal Access Token when Git prompts for a password. Do not put the token directly in the command or source files. If Git reports a non-fast-forward update, run `git pull --rebase origin main`, resolve conflicts, then repeat the push.

## 4. Build the APK from GitHub Actions

After the push succeeds, open the repository → **Actions** → **Build NOCTURNE Android APK**. Open the newest successful run and download **`NOCTURNE-4.2-debug-APK`** from Artifacts. Extract the artifact ZIP to get `app-debug.apk`.

You can also build locally where Gradle 8.13, JDK 17, Python 3.11 and Android SDK Platform 36 are installed:

```bash
cd ~/projects/NOCTURNE-Streaming-Android
gradle --no-daemon --stacktrace :app:assembleDebug
```

The resulting APK is `app/build/outputs/apk/debug/app-debug.apk`. The project archive is source code, not an APK installer.
