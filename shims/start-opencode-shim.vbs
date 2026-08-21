Option Explicit

' 开机自启：opencode anthropic shim（127.0.0.1:11437，含 ollama 分流）
' 与 start-shim.vbs（ollama, 11435）同款 pythonw 方案；pythonw 下 shim 的
' _log 会自动落到 ~/.claude/opencode_shim.log。

Dim shell, fso, userProfile, pythonw, scriptPath, command
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

userProfile = shell.ExpandEnvironmentStrings("%USERPROFILE%")
pythonw = userProfile & "\AppData\Local\hermes\hermes-agent\venv\Scripts\pythonw.exe"
If Not fso.FileExists(pythonw) Then pythonw = "pythonw.exe"

scriptPath = userProfile & "\.claude\opencode_anthropic_shim.py"
command = Quote(pythonw) & " " & Quote(scriptPath) & " --host 127.0.0.1 --port 11437"

shell.Run command, 0, False

Function Quote(value)
    Quote = Chr(34) & value & Chr(34)
End Function
