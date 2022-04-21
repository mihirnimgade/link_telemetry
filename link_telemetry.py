import requests
import serial
import sys
import influxdb_client
from influxdb_client.client.write_api import SYNCHRONOUS
from collections import OrderedDict
from pathlib import Path
import yaml
import pprint
from enum import Enum

# <----- Constants ----->

SERVER_NAME = "http://localhost:3000"
YAML_FILE = Path("can.yaml")
CAR_NAME = "Daybreak"

# <----- InfluxDB constants ----->

BUCKET = "Telemetry"
ORG = "UBC Solar"
TOKEN = "T_30GSM8YENk9XFXxmTnqohRO7KSNMfp7YDcf03wAShj0Ti7B3ORQrWwE8ZIDgaRvi-dwAdHSvsw3H1iOr2OUQ=="
URL = "http://localhost:8086"

# <----- Server start-ups ----->

# TODO: start InfluxDB server here
# TODO: start Grafana server here

# <----- Class definitions ------>


class CANMessage:
    EXPECTED_CAN_MSG_LENGTH = 30

    def __init__(self, raw_string):
        assert len(raw_string) == CANMessage.EXPECTED_CAN_MSG_LENGTH, \
            f"raw_string not expected length of {EXPECTED_CAN_MSG_LENGTH}"

        self.timestamp = int(raw_string[0:8].decode(), 16)        # 8 bytes
        self.identifier = int(raw_string[8:12].decode(), 16)      # 4 bytes
        self.data_len = int(raw_string[28:29].decode(), 16)       # 1 byte

        self.hex_identifier = hex(self.identifier)

        data = list(self.chunks(raw_string[12:28], 2))            # 16 bytes
        data = list(map(bytes.decode, data))

        # separated into bytes (each byte represented in decimal)
        self.data = list(map(lambda x: int(x, 16), data))

        # separated into bytes (each byte represented in binary)
        self.bytestream = list(map(lambda x: "{0:08b}".format(x), self.data))

        # single binary number representing the CAN message data
        self.bitstream = "".join(self.bytestream)

    def __repr__(self):
        """ Provides a string representation of the CAN message """

        repr_str = str()

        repr_str += f"{self.hex_identifier=}\n"
        repr_str += f"{self.timestamp=}\n"
        repr_str += f"{self.data_len=}\n"
        repr_str += f"{self.data=}\n"
        repr_str += f"{self.bytestream=}\n"
        repr_str += f"{self.bitstream=}\n"

        return repr_str

    def extract_measurements(self, schema: dict):
        """
        Extracts measurements from the CAN message depending on the entries in
        the `schema` dict. Returns a measurement dict with the key as the measurement name
        and the value as a dict containing data about the given measurement.

        Raises exception if schema does not contain key entry that matches `self.identifier`.
        """

        # retrieve schema data for CAN ID
        schema_data = schema.get(hex(self.identifier))

        if schema_data is None:
            raise ValueError(f"WARNING: Schema not found for id={hex(self.identifier)}, make entry in {YAML_FILE}\n")

        measurements = schema_data.get("measurements")

        # where the data came from
        source = schema_data.get("source")

        # the "name" of the CAN message
        measurement_class = schema_data.get("name")

        measurement_dict = dict()

        for name, data in measurements.items():
            bits = data["bits"]
            measurement_type = data["type"]

            measurement_dict[name] = dict()

            # extract measurement from CAN message bitstream

            # if only a single bound is provided, extract single bit
            if len(bits) == 1:
                bit_index = bits[0]
                extracted_value = self.bitstream[bit_index]

            # if both bounds are provided, extract range of bits
            if len(bits) == 2:
                lower = bits[0]
                upper = bits[1]

                # lower and upper are both inclusive bounds
                extracted_value = self.bitstream[lower:upper+1]

            # convert binary bitstream values to integers
            processing_fn = TYPE_PROCESSING_MAP.get(measurement_type)

            if processing_fn is None:
                raise ValueError(f"WARNING: no entry for {measurement_type} found in {TYPE_PROCESSING_MAP=}\n")

            processed_value = processing_fn(extracted_value)

            # place into measurement dictionary
            measurement_dict[name]["source"] = source
            measurement_dict[name]["class"] = measurement_class
            measurement_dict[name]["value"] = processed_value

        return measurement_dict

        """
        sample_dict = {
                "state_of_charge": {
                    "source": "",
                    "message_name": "",
                    "value": ""
                    }
                }
        """

    @staticmethod
    def chunks(lst, n):
        """Yield successive n-sized chunks from list."""

        for i in range(0, len(lst), n):
            yield lst[i: i+n]

    @staticmethod
    def twos_complement8(byte: str):
        """
        Interprets byte as two's complement signed integer.
        NOTE: Byte is assumed to be big-endian (MSB first)
        """

        assert len(byte) == 8, "`byte` argument must be length 8"

        sign = int(byte[0], 2)
        tail = int(byte[1:], 2)

        # if number is negative
        if sign == 1:
            # invert and add one
            invert_tail = 127 - tail
            value = invert_tail + 1
            return -1 * value

        return tail

    @staticmethod
    def twos_complement16(word: str):
        """
        Interprets word as two's complement signed integer.
        NOTE: Word is assumed to be big-endian (MSB first)
        """

        assert len(word) == 16, "`word` argument must be length 16"

        sign = int(word[0], 2)
        tail = int(word[1:], 2)

        # if number is negative
        if sign == 1:
            # invert and add one
            invert_tail = 32767 - tail
            value = invert_tail + 1
            return -1 * value

        return tail


TYPE_PROCESSING_MAP = {
        "bool": lambda x: True if int(x, 2) == 1 else False,
        "unsigned": lambda x: int(x, 2),
        "signed_8": CANMessage.twos_complement8,
        "signed_16": CANMessage.twos_complement16,
        "incremental": lambda x: int(x, 2) * 0.1
}

def main():
    # argument validation
    assert len(sys.argv) >= 2, "COM port not specified"
    assert len(sys.argv) >= 3, "Baudrate not specified"

    port = sys.argv[1]
    baudrate = sys.argv[2]

    pp = pprint.PrettyPrinter(indent=1)

    # <----- InfluxDB object set-up ----->

    client = influxdb_client.InfluxDBClient(url=URL, org=ORG, token=TOKEN)
    write_api = client.write_api(write_options=SYNCHRONOUS)

    # <----- Read in YAML CAN schema file ----->

    with open(YAML_FILE, "r") as f:
        can_schema: dict = yaml.safe_load(f)

    while True:
        with serial.Serial() as ser:
            # <----- Configure COM port ----->
            ser.baudrate = baudrate
            ser.port = port
            ser.open()

            # read in bytes from COM port
            message = ser.readline()

            if len(message) != CANMessage.EXPECTED_CAN_MSG_LENGTH:
                print(f"WARNING: got message length {len(message)}, expected {CANMessage.EXPECTED_CAN_MSG_LENGTH}. Dropping message...")
                continue

        can_msg = CANMessage(raw_string=message)

        # extract measurements from CAN message
        try:
            extracted_measurements = can_msg.extract_measurements(can_schema)

            # print parsed CAN messages and extracted measurement
            print(can_msg)
            pp.pprint(extracted_measurements)

        except ValueError as exc:
            print(exc)
            continue

        # write all measurements to InfluxDB database
        for measurement, data in extracted_measurements.items():
            # unpack measurement data
            source = data["source"]
            m_class = data["class"]
            value = data["value"]

            p = influxdb_client.Point(source).tag("car", CAR_NAME).tag("class", m_class).field(measurement, value)
            print(p)
        print()
            # write_api.write(bucket=BUCKET, org=ORG, record=p)


if __name__ == "__main__":
    main()
