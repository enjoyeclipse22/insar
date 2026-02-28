@echo off
echo Starting InSAR verification in WSL...
wsl -d Ubuntu-22.04 -e bash -c "cd /root/insar && export PATH=/root/gmtsar/bin:\$PATH && export GMTHOME=/root/gmtsar && nohup python3 process_insar_fixed.py > run_verify.log 2>&1 &"
timeout /t 5 >nul
echo Started. Check progress with: wsl tail -f /root/insar/run_verify.log
