#!/usr/bin/env python3

import usb.core
import usb.util
import attr
from time import sleep, time
from datetime import datetime, timezone
from lightmeter_table import jsonSchemaPrefix
import signal
from influxdb import InfluxDBClient
from math import exp

a = 1.4434e+05
b = 3.25274e-03
c = 1.3120e-08
d = 5.2776e-03

class GracefulKiller:
    """
    Class to react on termination signal from the operating system
    """
    kill_now = False

    def __init__(self):
        """
        connect the SIGINT and SIGTERM signals
        """
        signal.signal(signal.SIGINT, self.exit_gracefully)
        signal.signal(signal.SIGTERM, self.exit_gracefully)

    def exit_gracefully(self, signum, frame):
        """
        just sets the flag, that the process has to terminate now
        :param signum: will be ignored gracefully
        :param frame: will also be ignored gracefully
        :return:
        """
        self.kill_now = True



class Lightmeter:
    """An instance of a Kuffner-Sternwarte lightmeter. Call `read` to read the
    timestamped light levels."""

    @attr.s(frozen=True)
    class Reading:
        """A lightmeter reading.

        Reading is a read-only structure with the following fields:
            utc -- a `datetime` object representing the timestamp
            lightlevel -- the raw counts representing the light level
            daylight -- the reading of the daylight sensor in Lux
            temperature -- the temperature in degrees Celsius
            status -- True if everything was fine, False otherwise

        The daylight sensor is available for certain hardware models only.
        """
        utc = attr.ib()
        lightlevel = attr.ib()
        daylight = attr.ib()
        temperature = attr.ib()
        status = attr.ib()

        _colOrder = ("utc", "temperature", "lightlevel", "daylight", "status")
        _abbrevOrder = ("TS", "T", "L", "D", "S")

        def json(self, abbrev=False):
            dct = attr.asdict(self)
            dct['utc'] = '"' + self.utc.isoformat() + '"'
            dct['status'] = 'true' if self.status else 'false'
            order = self._abbrevOrder if abbrev else self._colOrder
            line = ', '.join(['"{}": {}'.format(y, dct[x])
                              for x, y in zip(self._colOrder, order)])
            return '{{{}}}'.format(line)

    def __init__(self):
        self._endpoints = Lightmeter._initDevice()

    def connect_db(self):
        """
        connect the client to the database
        :return:
        """
        self.client = InfluxDBClient('localhost', 8086,
                                     username='',
                                     password='',
                                     database='lightmeterdb')

        # if database does not exist, create the bmkdb. This does not overwrite an existing database
        self.client.create_database('lightmeterdb')

    def write_database(self, reading):
        """
        reads all variables from the input variable dictionary.
        compiles the JSON body
        write the data into the database
        :param variabledict: input variable dictionary
        :return:
        """
        json_body = [
            {
                "measurement": "Lightmeter",
                "tags": {
                    "host": "Mark 2.3 V05.05",
                    "serial": "908026.1447"
                },
                "time": reading.utc.isoformat(),
                "fields": {
                    "temperature": reading.temperature,
                    "lightlevel": reading.lightlevel,
                    "daylight": reading.daylight,
                    "lux": a * reading.lightlevel,
                    "watts":  c * (b * (a * exp(reading.lightlevel * (1.0 + d*reading.temperature)/a) - 1.0) + reading.lightlevel),
                    "status": reading.status
                }
            }
        ]
        self.client.write_points(json_body)

    def read(self):
        """Returns an instance of Lightmeter.Reading holding the current readings."""
        utc = datetime.now(timezone.utc)
        L, daylight, isOK = Lightmeter._readLight(self._endpoints)
        T = Lightmeter._readTemperature(self._endpoints)
        return Lightmeter.Reading(utc=utc, lightlevel=L,
                                  daylight=daylight, temperature=T,
                                  status=isOK)

    @staticmethod
    def _initDevice():
        """Finds a Microchip PICDEM, which is what the lightmeter identifies as,
        sadly. Not robust, but I can see no better way."""
        lightmeterParams = {
            'idVendor': 0x04d8,
            'idProduct': 0x000c,
            'configuration': 1,
            'interface': (0, 0),
            'reqLen': 64
        }

        # find our device
        dev = usb.core.find(idVendor=lightmeterParams['idVendor'],
                    idProduct=lightmeterParams['idProduct'])

        # was it found?
        if dev is None:
            raise RuntimeError('Device not found')

        # set the active configuration. With no arguments, the first
        # configuration will be the active one
        try:
            dev.set_configuration(lightmeterParams['configuration'])
        except usb.core.USBError as e:
            # if there are permission problems, this is where they manifest;
            # attach the bus and address so that outer code can print an
            # informative message.
            e.bus = dev.bus
            e.address = dev.address
            raise e

        # get an endpoint instance
        cfg = dev.get_active_configuration()
        intf = cfg[lightmeterParams['interface']]

        endpointOut = usb.util.find_descriptor(
            intf,
            # match the first OUT endpoint
            custom_match = lambda e: \
                usb.util.endpoint_direction(e.bEndpointAddress) \
                == usb.util.ENDPOINT_OUT)

        endpointIn = usb.util.find_descriptor(
            intf,
            # match the first IN endpoint
            custom_match = lambda e: \
                usb.util.endpoint_direction(e.bEndpointAddress) \
                == usb.util.ENDPOINT_IN)

        if endpointOut is None or endpointIn is None:
            raise RuntimeError('Unable to open endpoints')

        return endpointIn, endpointOut

    @staticmethod
    def _readTemperature(endpoints):
        endpointIn, endpointOut = endpoints
        N = endpointOut.write('T')
        if N != 1:
            raise RuntimeError('USB write error')
        raw = endpointIn.read(2)
        if len(raw) != 2:
            raise RuntimeError('USB read error')
        # Throw away 3 status bits and convert to decimal.
        return (raw[0] // 8 + raw[1] * 32) / 16

    @staticmethod
    def _luxFromDaysensor(Ch0, Ch1):
        """ Calculates Lux from the TAOS, www.taosinc.com TSL2560/TSL2561 two band light sensor
            for the TMB-package.
            Code from the Kuffner-Sternwarte web site.
        """
        if Ch0>0:
            Chr = Ch1 / Ch0
        else:
            return 0.0
        # Apply calibration recommended by manufacturer for different channel-ratios (IR-correction for vis-sensor to get Lux)
        if Chr <= 0.50:                        Lux=0.0304  *Ch0  - 0.062*Ch0*(Ch1/Ch0)**1.4
        elif (0.50 < Chr) and (Chr  <= 0.61):  Lux=0.0224  *Ch0  - 0.031  *Ch1
        elif (0.61 < Chr) and (Chr  <= 0.80):  Lux=0.0128  *Ch0  - 0.0153 *Ch1
        elif (0.80 < Chr) and (Chr  <= 1.30):  Lux=0.00146*Ch0  - 0.00112*Ch1
        elif 1.30 < Chr:                       Lux=0
        else: raise RuntimeError("Invalid daysensor channel ratio.")
        # calibration with Voltcraft handheld vs. Lightmeter Mark 2.3 No. L001 TAOS-daysensor
        Faktor = 21.0
        return Lux*Faktor

    @staticmethod
    def _readLight(endpoints):
        endpointIn, endpointOut = endpoints
        N = endpointOut.write('L')
        if N != 1:
            raise RuntimeError('USB write error')
        raw = endpointIn.read(7)
        if len(raw) != 7:
            raise RuntimeError('USB read error')
        factors = (None, 120, 8, 4, 2, 1)
        measurementRange = raw[2]
        TslMw0 = 256 * raw[4] + raw[3];
        TslMw1 = 256 * raw[6] + raw[5];
        rawReading = 256 * raw[1] + raw[0]
        reading = rawReading * factors[measurementRange]
        isOK = rawReading < 32000
        daylight = Lightmeter._luxFromDaysensor(TslMw0, TslMw1)
        return reading, daylight, isOK

class _MockLightmeter:
    """For testing."""

    def read(self):
        from random import randrange, choice
        """Returns an instance of Lightmeter.Reading holding the current readings."""
        utc = datetime.now(timezone.utc)
        return Lightmeter.Reading(utc=utc,
                                  lightlevel=randrange(1000, 100000),
                                  daylight=randrange(1,1000),
                                  temperature=float(randrange(-20,40)),
                                  status=choice((True, False)))

if __name__ == '__main__':
    import sys
    import argparse

    parser = argparse.ArgumentParser(description='Read light level from a '
                                                 'Kuffner-Sternwarte lightmeter '
                                                 'mark 2.3')
    parser.add_argument('-i', '--interval', type=float, default=1.0,
                        help='sampling interval in minutes (can be fractional)')
    parser.add_argument('--nohw', action='store_true',
                        help='don\'t use hardware and instead generate mock readings for testing')
    parser.add_argument('-f', '--format', default='text',
                        choices=('text', 'json_table', 'json_lines', 'json_lines_long', 'none'),
                        help='output format')

    args = parser.parse_args()

    try:
        if args.nohw:
            lmeter = _MockLightmeter()
        else:
            lmeter = Lightmeter()
    except usb.USBError as e:
        if e.errno != 13:
                raise e
        print(e, file=sys.stderr)
        print('Set read/write permissions on device node '
            '/dev/bus/usb/{:03d}/{:03d}'.format(e.bus,e.address),
            file=sys.stderr)
        print('Alternatively, use udev to fix this permanently.')
        exit(1)

    if args.format == 'text':
        print('# DATE_UTC TIME_UTC UNIX_EPOCH T_CELSIUS LIGHTMETER_COUNTS DAYLIGHT_LUX STATUS')
    elif args.format == 'json_table':
        import atexit
        print(jsonSchemaPrefix, end='')
        printComma = ''
        @atexit.register
        def finish():
            print('\n]}')

    lmeter.connect_db()
    killer = GracefulKiller()

    while not killer.kill_now:
        l = lmeter.read()
        if args.format == 'text':
            print(l.utc, int(l.utc.timestamp()),
                  '{:.1f}'.format(l.temperature), l.lightlevel,
                  '{:.3g}'.format(l.daylight),
                  ('OK' if l.status else 'ERROR'),
                  flush=True)
        elif args.format == 'json_lines':
            print(l.json(abbrev=True), flush=True)
        elif args.format == 'json_lines_long':
            print(l.json(abbrev=False), flush=True)
        elif args.format == 'json_table':
            print(printComma, l.json(abbrev=True), end='', sep='\n', flush=True)
            printComma = ','
        elif args.format == 'none':
            pass # maybe used for logger in future
        lmeter.write_database(l)
        sleep(args.interval)
