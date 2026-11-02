
;--------------------------------
; hi10_image_downloader Installer (Desktop Default)
;--------------------------------

!define APP_NAME "hi10_image_downloader"
!define APP_EXE "hi10_image_downloader.exe"
!define COMPANY_NAME "hi10_image_downloader"
!define INSTALL_DIR "$DESKTOP\hi10_image_downloader"

Name "${APP_NAME}"
OutFile "hi10_image_downloader_setup_${VERSION}.exe"
InstallDir "${INSTALL_DIR}"

; No admin rights needed
RequestExecutionLevel user

SetCompressor /SOLID lzma

Icon "..\icon.ico"
UninstallIcon "..\icon.ico"

;--------------------------------
; Pages
;--------------------------------

Page directory
Page instfiles

UninstPage uninstConfirm
UninstPage instfiles

;--------------------------------
; Install Section
;--------------------------------

Section "Install"

  ; Set output path
  SetOutPath "$INSTDIR"

  ; Copy all prepared files
  File /r "..\hi10_image_downloader_windows\*"

  ; Start Menu folder
  CreateDirectory "$SMPROGRAMS\hi10_image_downloader"

  CreateShortcut \
    "$SMPROGRAMS\hi10_image_downloader\hi10_image_downloader.lnk" \
    "$INSTDIR\${APP_EXE}"

  CreateShortcut \
    "$SMPROGRAMS\hi10_image_downloader\Uninstall hi10_image_downloader.lnk" \
    "$INSTDIR\uninstall.exe"

  ; Desktop shortcut
  CreateShortcut \
    "$DESKTOP\hi10_image_downloader.lnk" \
    "$INSTDIR\${APP_EXE}"

  ; Registry entries (HKCU - no admin needed)
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\hi10_image_downloader" "DisplayName" "${APP_NAME}"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\hi10_image_downloader" "UninstallString" "$INSTDIR\uninstall.exe"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\hi10_image_downloader" "DisplayIcon" "$INSTDIR\${APP_EXE}"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\hi10_image_downloader" "Publisher" "${COMPANY_NAME}"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\hi10_image_downloader" "DisplayVersion" "${VERSION}"
  WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\hi10_image_downloader" "NoModify" 1
  WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\hi10_image_downloader" "NoRepair" 1

  ; Write uninstaller
  WriteUninstaller "$INSTDIR\uninstall.exe"

SectionEnd

;--------------------------------
; Uninstall Section
;--------------------------------

Section "Uninstall"

  ; Delete shortcuts
  Delete "$DESKTOP\hi10_image_downloader.lnk"
  Delete "$SMPROGRAMS\hi10_image_downloader\hi10_image_downloader.lnk"
  Delete "$SMPROGRAMS\hi10_image_downloader\Uninstall hi10_image_downloader.lnk"
  RMDir "$SMPROGRAMS\hi10_image_downloader"

  ; Remove program files
  RMDir /r "$INSTDIR"

  ; Remove registry entries
  DeleteRegKey HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\hi10_image_downloader"

SectionEnd
