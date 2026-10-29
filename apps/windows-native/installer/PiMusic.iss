; PiMusic для Windows (нативный клиент) — установщик Inno Setup 6 (без прав администратора, в профиль пользователя).
; Сборка: ISCC /DAppVersion=1.1.0 /DSourceDir=<папка publish> PiMusic.iss  (см. apps/build/build-windows-native.ps1)
; Подписи кода нет: Windows покажет SmartScreen — «Подробнее» → «Выполнить в любом случае».
#ifndef AppVersion
  #define AppVersion "1.1.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\PiMusic\bin\publish"
#endif
#ifndef OutDir
  #define OutDir "..\..\dist"
#endif

[Setup]
AppId={{6F1D3C52-9B7A-4E0B-A2D4-5C8E71B3F9A6}
AppName=PiMusic
AppVersion={#AppVersion}
AppVerName=PiMusic {#AppVersion}
AppPublisher=PiMusic
DefaultDirName={localappdata}\Programs\PiMusic
DefaultGroupName=PiMusic
DisableProgramGroupPage=yes
DisableDirPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#OutDir}
OutputBaseFilename=PiMusic-{#AppVersion}-setup
SetupIconFile=..\PiMusic\Assets\icon.ico
UninstallDisplayIcon={app}\Assets\icon.ico
UninstallDisplayName=PiMusic
Compression=lzma2/ultra
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no
VersionInfoVersion={#AppVersion}
VersionInfoProductName=PiMusic

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
; AppUserModelID нужен для уведомлений и SMTC (должен совпадать с заданным в приложении)
Name: "{autoprograms}\PiMusic"; Filename: "{app}\PiMusic.exe"; AppUserModelID: "PiMusic.Desktop"
Name: "{autodesktop}\PiMusic"; Filename: "{app}\PiMusic.exe"; Tasks: desktopicon; AppUserModelID: "PiMusic.Desktop"

[Run]
; обычная установка — флажок «Запустить PiMusic»; тихая (автообновление) — запуск сразу
Filename: "{app}\PiMusic.exe"; Description: "Запустить PiMusic"; Flags: nowait postinstall skipifsilent
Filename: "{app}\PiMusic.exe"; Flags: nowait; Check: WizardSilent

; Данные пользователя ({localappdata}\PiMusic: настройки, кэш) при удалении не трогаем.
