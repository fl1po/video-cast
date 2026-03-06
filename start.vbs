CreateObject("Wscript.Shell").Run "pythonw " & Chr(34) & CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName) & "\app.py" & Chr(34), 0, False
