# Publish on GitHub with clicks

Use a clean extraction of the prepared **source ZIP** as the repository root and the **Windows x64 ZIP** for the downloadable app. Do not upload a copy you have already run: local settings and task history can be created there. These steps use the browser; no terminal is needed.

## Create the repository

1. Sign in to GitHub and select **+ → New repository**.
2. Choose your account, enter a name such as **rco-desktop**, and choose the visibility you want.
3. Leave the generated README, `.gitignore` and license options empty: the package already supplies the first two, and the application license is a separate choice.
4. Select **Create repository**. [GitHub's repository guide](https://docs.github.com/en/repositories/creating-and-managing-repositories/creating-a-new-repository)

The original application code has no selected license yet. Decide which license you want to offer before describing it as open source, and retain the included third-party notices. That choice is separate from the app's functionality.

## Upload the source

1. In File Explorer, choose **Extract All** for `rco-desktop-0.1.0-source.zip`.
2. Open the extracted folder until you see `README.md` beside `middleware`, `rco`, `asic` and `.github`.
3. On the empty repository page, choose **uploading an existing file**. For a repository with files, use **Add file → Upload files**.
4. Drag the **contents of that source folder** into the upload area. Keep the folders intact. Do not upload the ZIP itself or an extra enclosing `rco-desktop` folder.
5. Include `.github`, `.gitignore` and `.gitattributes`. Upload batches of at most 100 files if needed, always from the repository's root page so the folder structure stays correct.
6. Enter a short change description and complete the upload.

The repository's front page should now display this project's README. Under **Actions**, the **Checks** workflow runs automatically after the workflow file is present. Browser uploads allow up to 100 files per batch and 25 MiB per file. The executable belongs in the release assets below. [GitHub's file-upload guide](https://docs.github.com/en/repositories/working-with-files/managing-files/adding-a-file-to-a-repository)

## Add the downloadable Windows app

1. From the repository page, open **Releases → Draft a new release**.
2. Create the tag **v0.1.0**, select the branch containing the uploaded source, and title it **RCO Desktop 0.1.0**.
3. Describe the release as a Windows desktop prototype with a bounded ASIC configuration example. State the validation actually performed; do not imply live desktop or hardware tests that have not run.
4. In the release's file attachment area, add `rco-desktop-0.1.0-windows-x64.zip`, `rco-desktop-0.1.0-source.zip`, `SHA256SUMS.txt` and `VALIDATION.json` from the prepared package.
5. Select **This is a pre-release** for this prototype. Review the attachments, then choose **Publish release**. Use **Save draft** if you want to finish later. [GitHub's release guide](https://docs.github.com/en/repositories/releasing-projects-on-github/managing-releases-in-a-repository)

Readers download the Windows ZIP, extract it and double-click **RCO Middleware.exe**. Keep your own runtime state and desktop settings out of the uploaded files.
