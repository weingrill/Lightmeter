#!/bin/bash
cd /home/debian/Kuffner-Sternwarte-Lightmeter
source venv/bin/activate
python3 lightmeter_daemon.py
deactivate

if [ -s lightmeter.err ]; then
    mv -f lightmeter.err lightmeter.err.bak
    touch lightmeter.err
    echo file is not zero size, reboot commencing
#    sleep 60
#    /sbin/reboot
else
    echo no error detected
fi
