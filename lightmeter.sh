#!/bin/bash
if pgrep -x "python3" > /dev/null; then
    echo "running"
else
    cd /home/debian/Kuffner-Sternwarte-Lightmeter
    source venv/bin/activate
    python3 lightmeter_daemon.py
    deactivate
fi

if [ -s lightmeter.err ]; then
    mv -f lightmeter.err lightmeter.err.bak
    touch lightmeter.err
else
    echo no error detected
fi
