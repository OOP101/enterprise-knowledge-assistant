' 企业知识助手 —— 静默启动脚本
' 双击即可启动服务：无任何窗口弹出，服务在后台运行，日志见 server.log
' 开机自启：Win+R 输入 shell:startup，把本文件的快捷方式放进去即可
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = fso.GetParentFolderName(WScript.ScriptFullName)
sh.Run "python launcher.py", 0, False
