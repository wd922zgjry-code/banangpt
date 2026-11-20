' BananGPT: start without a console window (used by Windows autostart). Stop with stop.bat.
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = dir
sh.Environment("Process")("NOPAUSE") = "1"
sh.Run """" & dir & "\run.bat""", 0, False
