; FCAC installer (Inno Setup 6: https://jrsoftware.org/isinfo.php). Built by "Build EXE.bat" -> dist\FCAC-Setup-<version>.exe
#ifndef MyVersion
  #define MyVersion "1.0.2"
#endif

[Setup]
AppId={{6F1D2B7A-3C5E-4B8A-9E41-FCAC00000001}
AppName=FCAC
AppVersion={#MyVersion}
AppPublisher=sjames1066
AppPublisherURL=https://github.com/sjames1066/fcac
AppSupportURL=https://github.com/sjames1066/fcac
; per-user install: no administrator needed, and FCAC can write its config.json next to its program
PrivilegesRequired=lowest
DefaultDirName={localappdata}\Programs\FCAC
DefaultGroupName=FCAC
DisableProgramGroupPage=yes
UninstallDisplayIcon={app}\FCAC.exe
SetupIconFile=fcac.ico
OutputDir=dist
OutputBaseFilename=FCAC-Setup-{#MyVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes
RestartApplications=no

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Shortcuts:"
Name: "startwin"; Description: "Start FCAC quietly in the tray when I sign in to Windows"; GroupDescription: "Startup:"; Flags: unchecked

[Files]
Source: "dist\FCAC\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\FCAC"; Filename: "{app}\FCAC.exe"
Name: "{group}\Uninstall FCAC"; Filename: "{uninstallexe}"
Name: "{autodesktop}\FCAC"; Filename: "{app}\FCAC.exe"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "FCAC"; \
  ValueData: """{app}\FCAC.exe"" --background"; Flags: uninsdeletevalue; Tasks: startwin

[Run]
Filename: "{app}\FCAC.exe"; Description: "Start FCAC now"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{sys}\taskkill.exe"; Parameters: "/F /IM FCAC.exe"; Flags: runhidden; RunOnceId: "StopFCAC"

[Code]
// A running FCAC (tray) would keep its files locked: stop it before installing or updating.
function InitializeSetup(): Boolean;
var
  code: Integer;
begin
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM FCAC.exe', '', SW_HIDE, ewWaitUntilTerminated, code);
  Result := True;
end;

// Your recordings, packs and settings live in %LOCALAPPDATA%\FCAC and %LOCALAPPDATA%\FCAC data: they are left alone
// when you uninstall. Delete that folder yourself for a completely clean removal.
