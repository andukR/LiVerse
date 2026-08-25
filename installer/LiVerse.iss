#ifndef AppVersion
  #define AppVersion "1.1.0"
#endif

#ifndef SourceDir
  #define SourceDir "..\dist\LiVerse"
#endif

#ifndef OutputDir
  #define OutputDir "..\dist\installer"
#endif

[Setup]
AppId={{7318E792-31C0-4C6D-A356-4E78FE9EA378}
AppName=LiVerse
AppVersion={#AppVersion}
AppPublisher=LiVerse Project
AppPublisherURL=https://github.com/andukR/liverse
AppSupportURL=https://github.com/andukR/liverse/issues
DefaultDirName={localappdata}\Programs\LiVerse
DefaultGroupName=LiVerse
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir={#OutputDir}
OutputBaseFilename=LiVerse-Setup-{#AppVersion}
SetupIconFile=..\LiVerse.ico
UninstallDisplayIcon={app}\LiVerse.exe
Compression=lzma2/fast
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes
RestartApplications=no
UsePreviousAppDir=yes

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"

[Tasks]
Name: "desktopicon"; Description: "Создать ярлык на рабочем столе"; GroupDescription: "Дополнительные ярлыки:"; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{userprograms}\LiVerse"; Filename: "{app}\LiVerse.exe"; WorkingDir: "{app}"
Name: "{userdesktop}\LiVerse"; Filename: "{app}\LiVerse.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\LiVerse.exe"; Description: "Запустить LiVerse"; Flags: nowait postinstall skipifsilent
