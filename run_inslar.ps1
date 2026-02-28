# PowerShell script to run InSAR processing in WSL
$wslCommand = @"
cd ~/insar && export PATH=/root/gmtsar/bin:`$PATH && export GMTHOME=/root/gmtsar && python3 process_insar_fixed.py
"@

Write-Host "Starting InSAR processing in WSL..."
Write-Host "This will take approximately 30-60 minutes."
Write-Host ""

# Start the process
Start-Process -FilePath "wsl" -ArgumentList @("-d", "Ubuntu-22.04", "-e", "bash", "-c", $wslCommand) -NoNewWindow -Wait

Write-Host ""
Write-Host "Processing complete!"
