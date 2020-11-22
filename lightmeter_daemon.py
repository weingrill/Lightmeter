#!/usr/bin/env python3
import datetime as dt
import logging
import sys

import daemon
import usb.core

from lightmeter import Lightmeter

logging.basicConfig(filename='lightmeter.log',
                    level=logging.DEBUG,
                    format='%(asctime)s %(message)s')


def main_program():
    logging.debug('main_program()')
    lmeter = None
    try:
        logging.info('connecting lightmeter')
        lmeter = Lightmeter()
        logging.debug('connecting database')
        lmeter.connect_db()
    except usb.core.USBError as e:
        if e.errno != 13:
            raise e
        logging.exception(e, file=sys.stderr)
        logging.error('Set read/write permissions on device node '
                      '/dev/bus/usb/{:03d}/{:03d}'.format(e.bus, e.address),
                      file=sys.stderr)
        logging.error('Alternatively, use udev to fix this permanently.')
        exit(1)

    while True:
        starttime = dt.datetime.now()
        logging.debug('reading lightmeter')
        lightmeter_reading = lmeter.read()
        logging.debug('writing database')
        lmeter.write_database(lightmeter_reading)
        logging.debug('waiting for next cycle')
        while starttime + dt.timedelta(seconds=1) > dt.datetime.now():
            pass


if __name__ == '__main__':
    logging.info('Starting daemon…')
    stdin = open('/dev/null', 'rb')
    stdout = open('lightmeter.info', 'w+b')
    stderr = open('lightmeter.err', 'w+b', buffering=0)
    context = daemon.DaemonContext(uid=1000,
                                   gid=1000,
                                   stdin=stdin,
                                   stdout=stdout,
                                   stderr=stderr,
                                   detach_process=True)
    with context:
        logging.debug('in context')
        main_program()
    stdin.close()
    stdout.close()
    stderr.close()
    logging.debug('leaving context')
