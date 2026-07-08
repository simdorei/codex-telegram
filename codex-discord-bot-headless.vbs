Option Explicit

Dim fso
Dim shell
Dim scriptDir
Dim target
Dim tray
Dim logPath
Dim logFile
Dim stamp

Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
target = fso.BuildPath(scriptDir, "codex-discord-bot.cmd")
tray = fso.BuildPath(scriptDir, "codex-discord-tray.ps1")
logPath = fso.BuildPath(scriptDir, "discord_launcher.log")
stamp = Year(Now) & "-" & Pad2(Month(Now)) & "-" & Pad2(Day(Now)) & "T" & Pad2(Hour(Now)) & ":" & Pad2(Minute(Now)) & ":" & Pad2(Second(Now))

Set logFile = fso.OpenTextFile(logPath, 8, True)
logFile.WriteLine "[" & stamp & "] headless_launch target=" & target
logFile.Close

shell.Run """" & target & """", 0, False
If fso.FileExists(tray) Then
    shell.Run "powershell.exe -NoProfile -STA -ExecutionPolicy Bypass -WindowStyle Hidden -File " & Quote(tray), 0, False
End If

Function Pad2(value)
    If value < 10 Then
        Pad2 = "0" & value
    Else
        Pad2 = CStr(value)
    End If
End Function

Function Quote(value)
    Quote = """" & value & """"
End Function
