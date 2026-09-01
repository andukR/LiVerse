#ifndef AppVersion
  #define AppVersion "1.2.2"
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

[InstallDelete]
Type: files; Name: "{app}\LiVerseEngine.exe"

[Icons]
Name: "{userprograms}\LiVerse"; Filename: "{app}\LiVerse.exe"; WorkingDir: "{app}"
Name: "{userdesktop}\LiVerse"; Filename: "{app}\LiVerse.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\LiVerse.exe"; Description: "Запустить LiVerse"; Flags: nowait postinstall skipifsilent

[Code]
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  EnginePath: String;
  ResultCode: Integer;
begin
  Result := '';
  EnginePath := ExpandConstant('{app}\LiVerseEngine.exe');
  if not FileExists(EnginePath) then
    Exit;

  { Windows cannot replace a running executable. Stop every old LiVerse engine
    before explicitly removing the installed copy. A nonzero taskkill result
    also means that no matching process was running, so deletion is the final
    authoritative check. }
  Exec(
    ExpandConstant('{sys}\taskkill.exe'),
    '/F /T /IM LiVerseEngine.exe',
    '',
    SW_HIDE,
    ewWaitUntilTerminated,
    ResultCode
  );
  Sleep(500);

  if FileExists(EnginePath) and (not DeleteFile(EnginePath)) then
  begin
    Result :=
      'Не удалось остановить и удалить старый LiVerseEngine.exe.' + #13#10 +
      'Полностью закройте LiVerse и повторите установку.';
    Exit;
  end;

  if FileExists(EnginePath) then
    Result :=
      'Старый LiVerseEngine.exe не был удалён.' + #13#10 +
      'Установка остановлена, чтобы не смешивать разные версии LiVerse.';
end;
