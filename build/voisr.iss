; Voisr Inno Setup installer (Phase 5 Task 6).
;
; Per-user, unelevated install into {localappdata}\Programs\Voisr, matching
; the app's own design: it detects and reports UIPI blocking rather than
; demanding admin. Source tree is the PyInstaller one-folder bundle produced
; by build\voisr.spec (build\dist\Voisr by default, override with
; /DSourceDir=... on the ISCC command line).
;
; The launch-at-login Run value written here MUST match what
; voisr/launch_agent.py's enable() produces for a frozen build exactly
; (`"<exe path>"`, quoted, nothing else) -- see _launch_command() there.
; If these disagree, the Settings toggle and this installer's login task
; will silently overwrite each other's Run entry.

#ifndef SourceDir
  #define SourceDir "dist\Voisr"
#endif
#define MyAppName "Voisr"
#define MyAppVersion "1.0.0"
#define MyAppPublisher "Voisr"
#define MyAppExeName "Voisr.exe"
#define MyAppId "Voisr.Voisr"

[Setup]
AppId={#MyAppId}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\Programs\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
; Unelevated, per-user: never prompt for admin. This must stay lowest --
; the whole app is designed to run without elevation.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=commandline
LicenseFile=..\LICENSE
OutputDir=Output
OutputBaseFilename=VoisrSetup
SetupIconFile=..\assets\voisr.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
Compression=lzma2
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
WizardStyle=modern

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked
Name: "launchatlogin"; Description: "Start Voisr when I log in"; GroupDescription: "Startup:"; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion
; WebView2 Evergreen Bootstrapper, fetched by scripts\build_release.ps1. Not
; installed to {app} -- extracted to {tmp} and run only when the runtime is
; missing. dontcopy keeps it out of the installed tree entirely.
Source: "MicrosoftEdgeWebview2Setup.exe"; Flags: dontcopy noencryption

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; AppUserModelID: "{#MyAppId}"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{userdesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon; AppUserModelID: "{#MyAppId}"

; Launch-at-login: same HKCU\...\Run value name and the same quoted-exe-path
; command string that launch_agent.py's frozen _launch_command() writes.
[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "Voisr"; ValueData: """{app}\{#MyAppExeName}"""; Tasks: launchatlogin; Flags: uninsdeletevalue

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent

; Uninstall must remove only the program directory and the Run value --
; {app} is {localappdata}\Programs\Voisr, never {localappdata}\Voisr (the
; user's config and dictation history), so the default file-list-driven
; uninstall already can't touch it. No [UninstallDelete] entries are added
; here on purpose: adding one for {localappdata}\Voisr would be the bug.

[Code]
const
  WebView2ClientGuid = '{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';
  WebView2DownloadUrl = 'https://go.microsoft.com/fwlink/p/?LinkId=2124703';

function WebView2RuntimeInstalled(): Boolean;
var
  Version: String;
begin
  Result := False;
  if RegQueryStringValue(HKLM, 'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\' + WebView2ClientGuid, 'pv', Version) then begin
    if (Version <> '') and (Version <> '0.0.0.0') then begin
      Result := True;
      exit;
    end;
  end;
  if RegQueryStringValue(HKLM, 'SOFTWARE\Microsoft\EdgeUpdate\Clients\' + WebView2ClientGuid, 'pv', Version) then begin
    if (Version <> '') and (Version <> '0.0.0.0') then begin
      Result := True;
      exit;
    end;
  end;
  if RegQueryStringValue(HKCU, 'SOFTWARE\Microsoft\EdgeUpdate\Clients\' + WebView2ClientGuid, 'pv', Version) then begin
    if (Version <> '') and (Version <> '0.0.0.0') then begin
      Result := True;
      exit;
    end;
  end;
end;

// Install the WebView2 runtime ourselves rather than sending the user to a
// download page: without it the dashboard cannot render at all. The Evergreen
// Bootstrapper installs per-user when run unelevated, so this keeps the
// installer's no-admin promise. Already present on Windows 11 and on any PC
// with a recent Edge, where this is a no-op.
//
// PrepareToInstall (not InitializeWizard) so the work happens on the progress
// page and a failure can be reported through Setup's own error path.
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ExitCode: Integer;
begin
  Result := '';
  if WebView2RuntimeInstalled() then
    exit;

  WizardForm.StatusLabel.Caption := 'Installing the Microsoft Edge WebView2 Runtime...';
  ExtractTemporaryFile('MicrosoftEdgeWebview2Setup.exe');
  if not Exec(ExpandConstant('{tmp}\MicrosoftEdgeWebview2Setup.exe'),
              '/silent /install', '', SW_HIDE, ewWaitUntilTerminated, ExitCode) then begin
    Result := 'Could not start the WebView2 Runtime installer.';
    exit;
  end;
  if ExitCode <> 0 then begin
    Result := 'The WebView2 Runtime installer failed (exit code ' + IntToStr(ExitCode) + ').' + #13#10 +
      'It needs an internet connection. Install it manually from' + #13#10 +
      WebView2DownloadUrl + #13#10 + 'then run this setup again.';
    exit;
  end;
  // Trust the runtime's own detection over the exit code alone.
  if not WebView2RuntimeInstalled() then
    Result := 'The WebView2 Runtime still was not detected after installing it.' + #13#10 +
      'Install it manually from ' + WebView2DownloadUrl + ' then run this setup again.';
end;
