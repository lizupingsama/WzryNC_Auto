' WzryNC_Auto GUI silent launcher.
' If venv is ready, start the GUI with pythonw (no console window at all).
' Otherwise fall back to start_gui.bat (visible, for first-time setup).
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = dir
pyw = dir & "\venv\Scripts\pythonw.exe"
If fso.FileExists(pyw) Then
    sh.Run """" & pyw & """ """ & dir & "\wzry_gui.py""", 0, False
Else
    sh.Run """" & dir & "\start_gui.bat""", 1, False
End If
