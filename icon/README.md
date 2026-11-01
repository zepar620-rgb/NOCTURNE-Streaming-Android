# NOCTURNE launcher icon

The Android launcher icon is compiled from `icon/res/drawable/ic_nocturne.xml`.
This folder is added to the Android app resource source set in `app/build.gradle`,
so the manifest can reference it as `@drawable/ic_nocturne`.

`nocturne-icon.svg` is an editable reference/source design. To replace the actual
launcher icon, update `res/drawable/ic_nocturne.xml` with a valid Android
VectorDrawable, or add PNG assets in a `mipmap-*` resource folder and change the
manifest icon reference accordingly.
