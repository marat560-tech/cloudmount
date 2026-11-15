; Установщик CloudMount (Inno Setup 6). Собирается в GitHub Actions.
#define AppVersion GetEnv("CM_VERSION")
#if AppVersion == ""
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{F1A9E5C2-3B7D-4E8A-9C61-2D5B7A0E4C13}
AppName=CloudMount
AppVersion={#AppVersion}
AppVerName=CloudMount {#AppVersion}
AppPublisher=Stella
AppPublisherURL=https://github.com/marat560-tech/cloudmount
AppSupportURL=https://github.com/marat560-tech/cloudmount/issues
DefaultDirName={autopf}\CloudMount
DefaultGroupName=CloudMount
DisableProgramGroupPage=yes
UninstallDisplayIcon={app}\CloudMount.exe
UninstallDisplayName=CloudMount
OutputDir=..\dist
OutputBaseFilename=CloudMount-Setup
SetupIconFile=..\assets\icon.ico
LicenseFile=LICENSE-installer.txt
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=admin
CloseApplications=force
RestartApplications=no

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"

[Tasks]
Name: "winfsp"; Description: "Установить драйвер WinFsp (обязателен для работы дисков)"; Check: not WinFspInstalled
Name: "desktopicon"; Description: "Значок на рабочем столе"
Name: "autostart"; Description: "Запускать CloudMount вместе с Windows"

[Files]
Source: "..\dist\CloudMount.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\LICENSE"; DestDir: "{app}"; DestName: "LICENSE.txt"; Flags: ignoreversion
Source: "..\licenses\WinFsp-License.txt"; DestDir: "{app}\licenses"; Flags: ignoreversion
Source: "..\build\winfsp.msi"; DestDir: "{tmp}"; Flags: deleteafterinstall; Tasks: winfsp

[Icons]
Name: "{autoprograms}\CloudMount"; Filename: "{app}\CloudMount.exe"
Name: "{autodesktop}\CloudMount"; Filename: "{app}\CloudMount.exe"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "CloudMount"; ValueData: """{app}\CloudMount.exe"" --minimized"; Flags: uninsdeletevalue; Tasks: autostart

[Run]
Filename: "msiexec.exe"; Parameters: "/i ""{tmp}\winfsp.msi"" /qn /norestart"; StatusMsg: "Устанавливаю драйвер WinFsp…"; Tasks: winfsp; Flags: waituntilterminated
Filename: "{app}\CloudMount.exe"; Description: "Запустить CloudMount"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "taskkill.exe"; Parameters: "/F /IM CloudMount.exe"; Flags: runhidden; RunOnceId: "StopCloudMount"

[Code]
function WinFspInstalled: Boolean;
var
  Dir: String;
begin
  Result := RegQueryStringValue(HKLM32, 'SOFTWARE\WinFsp', 'InstallDir', Dir) or
            RegQueryStringValue(HKLM64, 'SOFTWARE\WinFsp', 'InstallDir', Dir);
end;
