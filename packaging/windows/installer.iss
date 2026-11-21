; Inno Setup script for BLOODBOS RADIO
; Built by the release workflow:  ISCC.exe /DAppVersion=1.0.0 packaging\windows\installer.iss
;
; Installs just for the current user (no administrator prompt), into
; %LOCALAPPDATA%\Programs\BLOODBOS Radio, with Start-menu and desktop shortcuts.
; Settings and logins live in %APPDATA%\BLOODBOS Radio and survive updates and
; uninstalling.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#define AppName "BLOODBOS Radio"
#define AppExe "BLOODBOS Radio.exe"

[Setup]
AppId={{6E0F4D2B-8C1A-4B7E-9A55-2D3C1B0B70D1}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=korn788
AppPublisherURL=https://github.com/korn788/BloodBos-music-request-bot
AppSupportURL=https://github.com/korn788/BloodBos-music-request-bot/issues
AppUpdatesURL=https://github.com/korn788/BloodBos-music-request-bot/releases
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
DisableDirPage=auto
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=..\..\dist
OutputBaseFilename=BLOODBOS-Radio-Setup-Windows
SetupIconFile=..\..\src\assets\icon.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes
RestartApplications=no
LicenseFile=..\..\LICENSE

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Put a shortcut on my desktop"; GroupDescription: "Shortcuts:"

[Files]
Source: "..\..\dist\{#AppName}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[InstallDelete]
; old files from a previous version of the app folder (settings are elsewhere)
Type: filesandordirs; Name: "{app}\_internal"
Type: filesandordirs; Name: "{app}\vlc"

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "Start {#AppName} now"; Flags: nowait postinstall skipifsilent
