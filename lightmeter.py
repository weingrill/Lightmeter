#!/usr/bin/env python3

import usb.core
import usb.util
import attr
import datetime as dt
import signal
from paho.mqtt import client as mqtt_client
import time

from math import exp
import logging
from logging.config import fileConfig
import sys
import argparse
from _config import bucket, org, token, url, mqtt_broker, mqtt_port, client_id

fileConfig('logging_config.ini')
logger = logging.getLogger()

a = 1.4434e+05
b = 3.25274e-03
c = 1.3120e-08
d = 5.2776e-03
lux_factor = 153.423

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
        self._endpoints = Lightmeter._init_device()
        self.suspend_time_utc = dt.datetime.now(dt.timezone.utc)

    def connect_mqtt(self):
        def on_connect(client, userdata, flags, rc):
            if rc == 0:
                logger.info("Connected to MQTT Broker!")
            else:
                logger.exception("Failed to connect, return code %d\n", rc)
        # Set Connecting Client ID
        self.mqtt_client = mqtt_client.Client(client_id)
        self.mqtt_client.on_connect = on_connect
        self.mqtt_client.connect(mqtt_broker, mqtt_port)

    def send_mqtt(self, reading):
        """sends MQTT telegram to broker"""
        msg = '{"Time":"%s","TSL2560":{"Illuminance":%d}}' % (reading.utc.isoformat(),reading.daylight)
        topic = "tele/Lightmeter/SENSOR"
        result = self.mqtt_client.publish(topic, msg)
        # result: [0, 1]
        status = result[0]
        if status == 0:
            logger.debug("MQTT message sent: ",msg)
        else:
            logger.exception(f"Failed to send MQTT message")

    def read(self):
        """Returns an instance of Lightmeter.Reading holding the current readings."""
        logger.info("read")
        utc = dt.datetime.now(dt.timezone.utc)
        try:
            lightlevel, daylight, is_ok = Lightmeter._read_light(self._endpoints)
        except RuntimeError as read_lightexception:
            logger.exception(read_lightexception)
            lightlevel = None
            daylight = None
            is_ok = False
        if utc >= self.suspend_time_utc:
            try:
                temperature, status = Lightmeter._read_temperature(self._endpoints)
            except RuntimeError as temperature_exception:
                logger.exception(temperature_exception)
                temperature = None
                is_ok = False
            else:
                self.suspend_time_utc = utc
        else:
            temperature = None
            logger.info("temperature suspended")
        if temperature is not None:
            if temperature > 60.0 or temperature < -30.0:
                temperature = None
        if temperature is not None:
            if temperature < 35.0:
                self.suspend_time_utc = utc
            else:   # wait for twelve hours
                self.suspend_time_utc = utc + dt.timedelta(hours=12)
            logger.warning('suspending temperature readout until %s', self.suspend_time_utc.isoformat())
        if daylight < 3.0:
            daylight = lightlevel / 145000.0
        return Lightmeter.Reading(utc=utc, lightlevel=lightlevel,
                                  daylight=daylight, temperature=temperature,
                                  status=is_ok)

    @staticmethod
    def _init_device():
        """Finds a Microchip PICDEM, which is what the lightmeter identifies as,
        sadly. Not robust, but I can see no better way."""
        logger.debug("_init_device")
        lightmeter_params = {
            'idVendor': 0x04d8,
            'idProduct': 0x000c,
            'configuration': 1,
            'interface': (0, 0),
            'reqLen': 64
        }

        # find our device
        dev = usb.core.find(idVendor=lightmeter_params['idVendor'],
                            idProduct=lightmeter_params['idProduct'])

        # was it found?
        if dev is None:
            raise RuntimeError('Device not found')

        # set the active configuration. With no arguments, the first
        # configuration will be the active one
        try:
            dev.set_configuration(lightmeter_params['configuration'])
        except usb.core.USBError as usb_exception:
            # if there are permission problems, this is where they manifest;
            # attach the bus and address so that outer code can print an
            # informative message.
            usb_exception.bus = dev.bus
            usb_exception.address = dev.address
            raise usb_exception

        # get an endpoint instance
        cfg = dev.get_active_configuration()
        intf = cfg[lightmeter_params['interface']]

        endpoint_out = usb.util.find_descriptor(
            intf,
            # match the first OUT endpoint
            custom_match=lambda cm: usb.util.endpoint_direction(cm.bEndpointAddress) == usb.util.ENDPOINT_OUT)

        endpoint_in = usb.util.find_descriptor(
            intf,
            # match the first IN endpoint
            custom_match=lambda cm: usb.util.endpoint_direction(cm.bEndpointAddress) == usb.util.ENDPOINT_IN)

        if endpoint_out is None or endpoint_in is None:
            raise RuntimeError('Unable to open endpoints')

        return endpoint_in, endpoint_out

    @staticmethod
    def _read_temperature(endpoints):
        """
        status = (bytelow & MaskStatusTemp) == 0        # Check if status bit == 0
        valraw = ((bytehigh << 8)  + bytelow ) >> 3     # Statusbits ausblenden
        val = 625.0 * valraw / 10000.0                  # Umrechnung von 1/16属 zu 1/10属

        return val, status

        """
        endpoint_in, endpoint_out = endpoints
        n = endpoint_out.write('T')
        if n != 1:
            raise RuntimeError('USB temperature write error %d' % n)
        raw = endpoint_in.read(2)
        if len(raw) != 2:
            raise RuntimeError('USB temperature read error %d' % len(raw))
        # Throw away 3 status bits and convert to decimal.
        logger.debug('_read_temperature() = %x%x' % (raw[1], raw[0]))
        raw_temp = raw[1] * 256 + raw[0]
        status = raw_temp & 7
        raw_temp = raw_temp >> 3
        temperature = raw_temp / 16
        if temperature > 127.0:
            temperature = temperature - (0x7FFF >> 3) / 16
        return temperature, status

    @staticmethod
    def _lux_from_daysensor(channel0, channel1):
        """ Calculates Lux from the TAOS, www.taosinc.com TSL2560/TSL2561 two band light sensor
            for the TMB-package.
            Code from the Kuffner-Sternwarte web site.
        """
        logger.debug("_lux_from_daysensor")
        global lux_factor
        if channel0 > 0:
            channel_ratio = channel1 / channel0
        else:
            return 0.0
        # Apply calibration recommended by manufacturer for different channel-ratios
        # (IR-correction for vis-sensor to get Lux)
        if channel_ratio <= 0.50:
            lux = 0.0304 * channel0 - 0.062 * channel0 * (channel1 / channel0) ** 1.4
        elif (0.50 < channel_ratio) and (channel_ratio <= 0.61):
            lux = 0.0224 * channel0 - 0.031 * channel1
        elif (0.61 < channel_ratio) and (channel_ratio <= 0.80):
            lux = 0.0128 * channel0 - 0.0153 * channel1
        elif (0.80 < channel_ratio) and (channel_ratio <= 1.30):
            lux = 0.00146 * channel0 - 0.00112 * channel1
        elif 1.30 < channel_ratio:
            lux = 0
        else:
            raise RuntimeError("Invalid daysensor channel ratio.")
        # calibration with Thies Clima US
        if lux * lux_factor > 120000.0:
            lux_factor *= 120000.0 / (lux*lux_factor)
            logger.info('setting new lux_factor = %f' % lux_factor)
            print(lux_factor)
        return lux * lux_factor

    @staticmethod
    def _read_light(endpoints):
        logger.debug("_read_light")
        endpoint_in, endpoint_out = endpoints
        try:
            n = endpoint_out.write('L')
        except usb.USBError:
            logger.exception('USB Error')
            return 0, 0, False
        if n != 1:
            raise RuntimeError('USB lightlevel write error')
        raw = endpoint_in.read(7)
        if len(raw) != 7:
            raise RuntimeError('USB lightlevel read error')
        factors = (None, 120, 8, 4, 2, 1)
        measurement_range = raw[2]
        low_word = 256 * raw[4] + raw[3]
        high_word = 256 * raw[6] + raw[5]
        raw_reading = 256 * raw[1] + raw[0]
        reading = raw_reading * factors[measurement_range]
        is_ok = raw_reading < 32000
        if not is_ok:
            logger.warning("flux > 32000 non-linear")
        daylight = Lightmeter._lux_from_daysensor(low_word, high_word)
        return reading, daylight, is_ok


if __name__ == '__main__':

    parser = argparse.ArgumentParser(description='Read light level from a '
                                                 'Kuffner-Sternwarte lightmeter '
                                                 'mark 2.3')
    parser.add_argument('-i', '--interval', type=float, default=10.0,
                        help='sampling interval in minutes (can be fractional)')
    parser.add_argument('--nohw', action='store_true',
                        help='don\'t use hardware and instead generate mock readings for testing')
    parser.add_argument('--debug', action='store_true',
                        help='enable USB debug mode')
    parser.add_argument('-f', '--format', default='text',
                        choices=('text', 'log', 'none'),
                        help='output format')

    args = parser.parse_args()
    if args.debug:
        PYUSB_DEBUG = 'debug'

    lmeter = None
    printComma = ''
    try:
        lmeter = Lightmeter()
    except usb.USBError as e:
        if e.errno != 13:
            raise e
        print(e, file=sys.stderr)
        print('Set read/write permissions on device node '
              '/dev/bus/usb/{:03d}/{:03d}'.format(e.bus, e.address),
              file=sys.stderr)
        print('Alternatively, use udev to fix this permanently.')
        exit(1)

    if args.format == 'text':
        print('# DATE_UTC TIME_UTC UNIX_EPOCH T_CELSIUS LIGHTMETER_COUNTS DAYLIGHT_LUX STATUS')
        lmeter.connect_mqtt()

    killer = GracefulKiller()

    def none_str_fmt(value, format_string):
        if value is None:
            return 'N/A'
        return format_string.format(value)

    while not killer.kill_now:
        starttime = dt.datetime.now()
        l = lmeter.read()
        if args.format == 'text':
            print(l.utc,
                  int(l.utc.timestamp()),
                  none_str_fmt(l.temperature, '{:.1f}'),
                  l.lightlevel,
                  none_str_fmt(l.daylight, '{:8.1f}'),
                  ('OK' if l.status else 'ERROR'),
                  flush=True)
            lmeter.send_mqtt(l)
        elif args.format == 'log':
            print(l.lightlevel, flush=True)
            logger.info("%d", l.lightlevel)
        time.sleep(args.interval)
