; AURA installer (Inno Setup 6). Built automatically by the GitHub release
; workflow, or by build.bat when Inno Setup is installed.
;   iscc /DAppVersion=1.2.0 installer.iss   ->  dist\AURA-Setup.exe
;
; Installs for the current user only (no admin prompt), so AURA can update
; itself silently. Your library lives in %APPDATA%\Unreleased and is never
; touched by installing, updating or uninstalling.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
; Keep this AppId the same forever - it is how Windows knows a new installer
; is an update of the same app.
AppId={{6F2B8C1E-4A7D-4E5B-9C3A-2D1F0E8B7A64}
AppName=AURA
AppVersion={#AppVersion}
AppVerName=AURA {#AppVersion}
AppPublisher=Evan
DefaultDirName={localappdata}\Programs\AURA
DefaultGroupName=AURA
DisableProgramGroupPage=yes
DisableDirPage=auto
PrivilegesRequired=lowest
OutputDir=dist
OutputBaseFilename=AURA-Setup
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\AURA.exe
UninstallDisplayName=AURA
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no

[InstallDelete]
; Clear the previous version's program files first, so nothing stale is left behind.
; (Your library is in %APPDATA%\Unreleased and is not touched.)
Type: filesandordirs; Name: "{app}\_internal"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"

[Files]
; The whole program folder (AURA.exe plus its _internal folder). Already unpacked on
; disk, so AURA opens straight away instead of unpacking itself every time.
Source: "dist\AURA\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\AURA"; Filename: "{app}\AURA.exe"
Name: "{autodesktop}\AURA"; Filename: "{app}\AURA.exe"; Tasks: desktopicon

[Run]
; Opens AURA after a normal install. A self-update (silent) is reopened by AURA's
; own update helper instead, so this is skipped then.
Filename: "{app}\AURA.exe"; Description: "Open AURA"; Flags: nowait postinstall skipifsilent
