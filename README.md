# Cape Cup Map Editor and GitHub Pages Site

The local Python editor creates the final view-only `map.svg`, saves the editable project copy, and publishes only `map.svg` to Cloudflare R2. The GitHub Pages site displays the public R2 object and checks for updates about every two seconds, so later map changes do not require another Git commit or Pages deployment.

## Files

- `map_editor.py` — local editor and R2 publisher.
- `Cape Cup Map - Editor Source.svg` — initial editable map.
- `editable-map.svg` — editable project copy; this is never uploaded to R2.
- `map.svg` — local published copy retained for backup; the website loads the R2 copy.
- `index.html` — static GitHub Pages page.
- `requirements.txt` — Python dependency list.
- `.env.example` — required setting names without credentials.
- `.env` — settings saved by the local editor; created automatically and ignored by Git.

## 1. Create and expose the Cloudflare R2 bucket

1. In the Cloudflare dashboard, open **R2 Object Storage** and create a bucket, for example `cape-cup-map`.
2. Open **Manage R2 API Tokens** and create an S3 API token with **Object Read & Write** access limited to that bucket. Save the access key ID and secret access key when shown.
3. Copy the account-specific S3 endpoint. It normally has this form:

   ```text
   https://ACCOUNT_ID.r2.cloudflarestorage.com
   ```

4. Make the bucket publicly readable by connecting a custom domain or enabling the R2 public development URL (`r2.dev`) in the bucket settings. Cloudflare intends `r2.dev` for non-production testing and may rate-limit it; use a custom domain for the public game site.
5. Determine the complete public URL for the fixed object key `map.svg`, for example:

   ```text
   https://maps.example.com/map.svg
   ```

   The object is created by the editor's first successful publish. Before that, the site has no R2 map to display.

Do not use the public URL or public bucket controls as upload credentials. The access key and secret stay only on the computer running the editor.

## 2. Install Python dependencies

Windows PowerShell:

```powershell
py -m pip install -r requirements.txt
```

macOS or Linux:

```bash
python3 -m pip install -r requirements.txt
```

## 3. Configure R2 in the map editor

Start the editor, open **R2 publishing settings** in the left panel, enter all five values, and click **Save R2 settings**. The editor stores them in a local `.env` file beside `map_editor.py` and loads that file automatically the next time it starts.

The access key and secret fields are blank after saving. Leave them blank to keep the saved credentials, or enter new values to replace them. The `.env` file is ignored by Git and is not included in the exported website folder or ZIP.

The editor requires these five settings:

```text
R2_ENDPOINT_URL
R2_ACCESS_KEY_ID
R2_SECRET_ACCESS_KEY
R2_BUCKET_NAME
R2_PUBLIC_MAP_URL
```

You do not need to set them in Terminal when using the editor form. As an alternative, they can still be supplied for the current Windows PowerShell session:

```powershell
$env:R2_ENDPOINT_URL="https://ACCOUNT_ID.r2.cloudflarestorage.com"
$env:R2_ACCESS_KEY_ID="..."
$env:R2_SECRET_ACCESS_KEY="..."
$env:R2_BUCKET_NAME="cape-cup-map"
$env:R2_PUBLIC_MAP_URL="https://maps.example.com/map.svg"
```

These `$env:` values last only until that PowerShell window is closed.

For persistent Windows variables, use `setx` for each value:

```powershell
setx R2_ENDPOINT_URL "https://ACCOUNT_ID.r2.cloudflarestorage.com"
setx R2_ACCESS_KEY_ID "..."
setx R2_SECRET_ACCESS_KEY "..."
setx R2_BUCKET_NAME "cape-cup-map"
setx R2_PUBLIC_MAP_URL "https://maps.example.com/map.svg"
```

Close and reopen PowerShell after `setx`. Alternatively, use **System Properties → Advanced → Environment Variables** and add the same five user variables.

For the current macOS or Linux shell:

```bash
export R2_ENDPOINT_URL="https://ACCOUNT_ID.r2.cloudflarestorage.com"
export R2_ACCESS_KEY_ID="..."
export R2_SECRET_ACCESS_KEY="..."
export R2_BUCKET_NAME="cape-cup-map"
export R2_PUBLIC_MAP_URL="https://maps.example.com/map.svg"
```

Never commit a `.env` file or real credentials. `.gitignore` excludes credential files, while `.env.example` documents only the required names.

## 4. Configure GitHub Pages once

Near the bottom of `index.html`, replace:

```javascript
const MAP_URL = "REPLACE_WITH_PUBLIC_R2_MAP_URL";
```

with the complete value of `R2_PUBLIC_MAP_URL`. Replace the same placeholder in the `<img src="...">` attribute. Commit and push this one-time configuration to GitHub Pages.

The page keeps the last successfully loaded image on screen, preloads replacements to prevent flicker, refreshes every 2,000 milliseconds while visible, stops its timer while hidden, and refreshes immediately when visibility or window focus returns.

## 5. Edit and publish a map

Start the editor from the repository folder.

Windows:

```powershell
py map_editor.py
```

macOS or Linux:

```bash
python3 map_editor.py
```

Then:

1. To continue earlier work, click **Open previous map** and select `editable-map.svg` from the latest project folder.
2. Edit region colors and text.
3. Click **Publish map & export folder** once.
4. Wait for the success message. Success is shown only after R2 confirms the upload.

The publish action atomically writes the complete local `map.svg`, writes `editable-map.svg`, and then uploads only the published `map.svg` to the fixed R2 key `map.svg` with:

```text
Content-Type: image/svg+xml
Cache-Control: no-cache, max-age=0, must-revalidate
```

If configuration, authentication, internet access, or R2 fails, the editor reports the error and keeps the completed local files. Repeated clicks cannot start overlapping publishes.

## 6. Verify updates without a Git commit

1. Open the GitHub Pages site and leave it visible.
2. Publish a visibly changed map from the editor.
3. Confirm the editor reports a completed R2 upload.
4. Open `R2_PUBLIC_MAP_URL` directly and confirm the new map appears.
5. Within approximately two seconds, confirm the already-open GitHub Pages site changes without a Git commit or Pages deployment.
6. Temporarily disable the network or use an invalid map URL and confirm the page retains the last successfully displayed image.

Each open browser polls the same public object independently. The timestamp query parameter and object cache metadata allow multiple browsers and long-running tabs to see updates without exposing credentials.

## Cloudflare cache troubleshooting

The editor uploads `map.svg` with revalidation metadata, and the website requests it with a timestamp query parameter. Do not configure immutable or long-duration caching for this object.

If Cloudflare still serves an older file, create a Cloudflare Cache Rule for the public map hostname that bypasses caching when the URI path equals:

```text
/map.svg
```

Keep the rule limited to this object so other site assets can still use normal caching.
