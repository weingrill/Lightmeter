#!/usr/bin/env python3
import daemon
import logging
import usb.core
from time import sleep
from lightmeter import Lightmeter
import sys

logging.basicConfig(filename='lightmeter.log',
                    level=logging.DEBUG,
                    format='%(asctime)s %(message)s')

def main_program():
    logging.debug('main_program()')
    try:
        logging.info('connecting lightmeter')
        lmeter = Lightmeter()
    except usb.core.USBError as e:
        if e.errno != 13:
            raise e
        logging.exception(e, file=sys.stderr)
        logging.error('Set read/write permissions on device node '
            '/dev/bus/usb/{:03d}/{:03d}'.format(e.bus,e.address),
            file=sys.stderr)
        logging.error('Alternatively, use udev to fix this permanently.')
        exit(1)
    logging.debug('connecting database')
    lmeter.connect_db()
#    killer = GracefulKiller()

#    while not killer.kill_now:
    while True:
        logging.debug('reading lightmeter')
        l = lmeter.read()
        logging.debug('writing database')
        lmeter.write_database(l)
        logging.debug('waiting for next cycle')
        #sleep(0.5)


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
