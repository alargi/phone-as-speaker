' ============================================================
'  phone-as-speaker - hidden launcher
'
'  Double-click this file to start the service with NO terminal
'  window. It calls run.bat --silent, which switches to
'  pythonw.exe so that no console is ever created, and routes
'  all output into speaker.log.
'
'  To stop the service:
'    - click the "quit" button in the browser console, or
'    - double-click stop.bat.
'
'  NOTE: keep this file pure ASCII. wscript reads .vbs using the
'  system ANSI codepage, so any non-ASCII text here would render
'  as mojibake. The real (localized) messages live in speaker.log.
' ============================================================

Option Explicit

Dim fso, sh, base, bat, logFile, rc, msg

Set fso = CreateObject("Scripting.FileSystemObject")
Set sh  = CreateObject("WScript.Shell")

' Folder of this script, with a trailing backslash.
base    = fso.GetParentFolderName(WScript.ScriptFullName) & "\"
bat     = base & "run.bat"
logFile = base & "speaker.log"

If Not fso.FileExists(bat) Then
    MsgBox "run.bat was not found next to this script:" & vbCrLf & _
           bat & vbCrLf & vbCrLf & _
           "Please keep all files of the speaker folder together.", _
           16, "phone-as-speaker"
    WScript.Quit 1
End If

' 0 = hidden window. True = wait for the service to exit, so that
' we can report a startup failure instead of failing silently.
rc = sh.Run("cmd /c call """ & bat & """ --silent", 0, True)

If rc = 0 Then
    ' Normal shutdown (quit button / stop.bat). Nothing to report.
    WScript.Quit 0
End If

Select Case rc
    Case 1
        msg = "No Python interpreter was found." & vbCrLf & vbCrLf & _
              "Install Python 3.9 or newer and make sure it is on PATH," & vbCrLf & _
              "or run setup.bat once on a machine that has Python."
    Case 2
        msg = "Python was found, but the required dependencies are missing." & vbCrLf & vbCrLf & _
              "Double-click setup.bat once, then start this again."
    Case Else
        msg = "The speaker service stopped unexpectedly (exit code " & rc & ")." & vbCrLf & vbCrLf & _
              "It may already be running in another window, or the port" & vbCrLf & _
              "may be taken by another program." & vbCrLf & _
              "See speaker.log for the detailed error."
End Select

MsgBox msg, 48, "phone-as-speaker"

' Surface the real, localized error text from the log.
If fso.FileExists(logFile) Then
    sh.Run "notepad.exe """ & logFile & """", 1, False
End If

WScript.Quit rc
