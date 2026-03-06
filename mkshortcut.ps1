$ws = New-Object -ComObject WScript.Shell
$s = $ws.CreateShortcut("D:\Desktop\YT Chromecast.lnk")
$s.TargetPath = "C:\Users\Igor\yt-chromecast\start.vbs"
$s.WorkingDirectory = "C:\Users\Igor\yt-chromecast"
$s.Description = "Start YT Chromecast Controller"
$s.Save()
Remove-Item $MyInvocation.MyCommand.Source
