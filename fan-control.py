#!/usr/bin/env python3

'''

Script to parse and process IPMI information from SuperMicro X8/9/10/11 boards and intelligently adjust fan PWM.
Written by JBG 20190715

*** PLEASE READ THE README FILE FOR USAGE INFORMATION ***

*** I TAKE NO RESPONSIBILITY FOR ANY DAMAGES THAT MAY OCCUR FROM USING THIS SCRIPT. NO WARRANTY WHATSOEVER ***

'''

# Import required modules
import os, sys, re, time, signal
from subprocess import Popen, PIPE
import shutil

# Declarative configuration mapping: (INI Section, list of (INI Option, Global Variable Name, Default Value, [Type]))
# If Type is omitted, it's inferred from the default value.
# If Default is omitted, it's inferred from the current global variable state.
CONFIG_MAP = [
	('Fan Zone A', [
		('Sensor Name Search',          'ZONE_A_SENSOR_NAME_SEARCH', r'^.*CPU.*$'),
		('Sensor Test Match',           'ZONE_A_SENSOR_TEST_MATCH',  False, bool),
		('Minimum Temperature Degrees', 'ZONE_A_MIN_TEMP',           50),
		('Minimum Temperature Fan PWM', 'ZONE_A_MIN_FAN_PWM',        80),
		('Maximum Temperature Degrees', 'ZONE_A_MAX_TEMP',           60),
		('Maximum Temperature Fan PWM', 'ZONE_A_MAX_FAN_PWM',        100),
	]),
	('Fan Zone B', [
		('Sensor Name Search',          'ZONE_B_SENSOR_NAME_SEARCH', r'^.*CPU.*$'),
		('Sensor Test Match',           'ZONE_B_SENSOR_TEST_MATCH',  True, bool),
		('Minimum Temperature Degrees', 'ZONE_B_MIN_TEMP',           50),
		('Minimum Temperature Fan PWM', 'ZONE_B_MIN_FAN_PWM',        80),
		('Maximum Temperature Degrees', 'ZONE_B_MAX_TEMP',           60),
		('Maximum Temperature Fan PWM', 'ZONE_B_MAX_FAN_PWM',        100),
	]),
	('General Configuration', [
		('Poll Rate',                 'POLL_RATE',                 5),
		('Ignore Temp Change Amount', 'IGNORE_TEMP_CHANGE_AMOUNT', 1),
		('Temp Averaging Window',     'AVERAGE_WINDOW',            5),
		('Restore Fans On Exit',      'RESTORE_FANS_ON_EXIT',      True,  bool),
		('Exit On IPMI Failure',      'EXIT_ON_FAILURE',           False, bool),
		('Debug Mode',                'DEBUG',                     False, bool),
		('IPMITOOL',                  'IPMITOOL',                  False, str), # Default None, type str
	]),
]

# For command line arguments and internal state
CONFIG_TEST = False
PREV_CONFIG_MTIME = None
TERSE_OUTPUT = 0

# Compatibility shims for Python 2.7
try:
	import configparser
except ImportError:
	import ConfigParser as configparser

try:
	import statistics
except ImportError:
	class statistics:
		@staticmethod
		def mean(data):
			return sum(data) / float(len(data)) if data else 0.0

if sys.version_info[0] < 3:
	FileNotFoundError = IOError
	PermissionError = OSError

def which_compat(cmd):
	if hasattr(shutil, 'which'):
		return shutil.which(cmd)
	for path in os.environ.get("PATH", "").split(os.pathsep):
		full = os.path.join(path, cmd)
		if os.access(full, os.X_OK) and not os.path.isdir(full):
			return full
	return None

class ConfigWrapper:
	"""Helper to wrap configparser and provide defaults/type coercion."""
	def __init__(self, cfg, config_map):
		self.config = cfg
		self.config_map = config_map
		self.section = None

	def useSection(self, section):
		"""Set the active section for subsequent get calls."""
		self.section = section

	def get(self, option, default=None):
		try:
			val = self.config.get(self.section, option)
		except (configparser.NoSectionError, configparser.NoOptionError):
			if CONFIG_TEST: raise
			val = default
		return val

	def getint(self, option, default=None):
		try:
			return int(self.config.get(self.section, option))
		except (configparser.NoSectionError, configparser.NoOptionError, ValueError):
			if CONFIG_TEST: raise
			return default

	def getbool(self, option, default=None):
		try:
			val_str = self.config.get(self.section, option)
			if val_str is None:
				return default

			val_str_lower = str(val_str).lower()
			if val_str_lower in ["yes", "true", "1"]:
				return True
			elif val_str_lower in ["no", "false", "0"]:
				return False
			else:
				raise ValueError("Invalid boolean value: '%s'" % val_str)
		except (configparser.NoSectionError, configparser.NoOptionError, ValueError):
			if CONFIG_TEST: raise
			return default

	def has_option(self, option):
		return self.config.has_option(self.section, option)

	def load(self):
		# Declaratively load all mapped configuration values
		for section, section_options in self.config_map:
			self.useSection(section)
			for item in section_options:
				option, g_name = item[0], item[1]
				default = item[2] if len(item) >= 3 else globals().get(g_name)
				v_type = item[3] if len(item) == 4 else type(default)

				if v_type is bool: globals()[g_name] = self.getbool(option, default)
				elif v_type is int: globals()[g_name] = self.getint(option, default)
				else: globals()[g_name] = self.get(option, default)

def load_defaults(config_map):
	# Load the default configuration values
	for _, section_options in config_map:
		for item in section_options:
			# _, g_name, default, _
			globals()[item[1]] = item[2]

def get_default(target):
	for section, section_options in CONFIG_MAP:
		for (_, g_name, default, _) in section_options:
			if g_name == target:
				return default
	return None

def reload_config():
	# type: () -> None
	"""Reload the configuration file and update global settings."""

	# Only declare globals that are modified outside of config.load()
	global IPMITOOL, PREV_CONFIG_MTIME, POLL_RATE, IGNORE_TEMP_CHANGE_AMOUNT, AVERAGE_WINDOW

	# Prioritize system-wide config over local config
	config_path = '/etc/fan-control.ini'
	if not os.path.exists(config_path):
		config_path = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'config.ini')

	# Check if config.ini has been updated
	if os.path.exists(config_path) == False:
		if PREV_CONFIG_MTIME is None:
			print('WARNING: No configuration found. Assuming default values for now.')
			return
		else:
			print('Configuration file has disappeared; using last known configuration values.')
			return
	config_mtime = os.path.getmtime(config_path)
	if PREV_CONFIG_MTIME == config_mtime:
		return
	if PREV_CONFIG_MTIME is not None:
		# Ensure the file is at least 1 second old to avoid partial reads during writes
		if (time.time() - config_mtime) < 1.0:
			return
		sys.stdout.write('Configuration has changed. Reloading... ')
		sys.stdout.flush()

	config = configparser.ConfigParser()
	config.read(config_path)
	config = ConfigWrapper(config, CONFIG_MAP)
	config.load()

	# validate logic for safety
	if not CONFIG_TEST:
		if POLL_RATE < 1: POLL_RATE = 1
		if IGNORE_TEMP_CHANGE_AMOUNT < 0: IGNORE_TEMP_CHANGE_AMOUNT = 0
		if AVERAGE_WINDOW < 1: AVERAGE_WINDOW = 5

	ipmitool_bin = None
	ipmitool_desc = None

	# Process IPMITOOL overrides
	if IPMITOOL:
		if str(IPMITOOL).lower() in [ "", "0", "false", "none" ]:
			IPMITOOL = False
		else:
			# validate the external command exists, or replace with False
			ipmitool_bin = which_compat(IPMITOOL)
			if ipmitool_bin is None:
				err = "Unable to find ipmitool in system path: " + str(IPMITOOL)
				if CONFIG_TEST:
					raise FileNotFoundError(err)
				if DEBUG:
					sys.stdout.write("\n\nError: " + err + "\n")
				IPMITOOL = False

	# if false, use the builtin IPMICFG tool
	if IPMITOOL == False:
		ipmitool_bin = get_bundled_ipmicfg_binary()
		ipmitool_desc = "bundled IPMICFG tool"
	else:
		ipmitool_desc = "external IPMITOOL"
	if not ipmitool_bin:
		err = "Unable to find any IPMI helper while trying to find " + ipmitool_desc
		if CONFIG_TEST:
			raise FileNotFoundError(err)
		if DEBUG:
			sys.stdout.write("\n\nError: " + err + "\n")
	elif not os.path.isfile(ipmitool_bin):
		err = "Unable to find " + ipmitool_desc + " at: " + ipmitool_bin
		if CONFIG_TEST:
			raise FileNotFoundError(err)
		if DEBUG:
			sys.stdout.write("\n\nError: " + err + "\n")
	elif not os.access(ipmitool_bin, os.X_OK):
		err = "Unable to execute " + ipmitool_desc + " at: " + ipmitool_bin
		if CONFIG_TEST:
			raise PermissionError(err)
		if DEBUG:
			sys.stdout.write("\n\nError: " + err + "\n")
	elif DEBUG and IPMITOOL:
		sys.stdout.write("\nUsing ipmitool: " + IPMITOOL + "\n")

	PREV_CONFIG_MTIME = config_mtime
	if DEBUG: sys.stdout.write("done\n")

def get_bundled_ipmicfg_binary():
	# type: () -> str
	"""Return the path to the bundled IPMICFG binary."""
	return os.path.join(os.path.dirname(__file__), "./ipmitool/", "IPMICFG-Linux.x86")

def call_ipmi(params):
	# type: (list) -> list
	"""Execute an IPMI command and return the exit code and output."""
	global IPMITOOL
	if IPMITOOL:
		# External ipmitool prefers commands without the leading dash
		IPMICWD = os.path.dirname(__file__)
		IPMICMD = IPMITOOL
		if params[0] in ["-raw", "-sdr"]:
			params[0] = params[0][1:]
		else:
			if DEBUG:
				sys.stdout.write('Unknown params[0]: ' + params[0] + '\n')
				sys.stdout.flush()
			err = "Error: Unknown argument in call to external ipmitool: " + params[0]
			return [-1, '', err]
	else:
		# Bundled ipmicfg tool logic
		IPMICMD = get_bundled_ipmicfg_binary()
		IPMICWD = os.path.dirname(IPMICMD)

	IPMICMD = [IPMICMD]	+ params
	if DEBUG: sys.stdout.write(' ' + ' '.join(IPMICMD) + '\n')
	process = Popen(IPMICMD, stdout=PIPE, cwd=IPMICWD)
	(output, err) = process.communicate()
	EXITCODE = process.wait()
	if DEBUG: sys.stdout.write("IPMI exit code: %d\n" % EXITCODE)
	return [EXITCODE, output.decode('utf-8'), err]

def check_if_already_running():
	# type: () -> None
	"""Exit if another instance of this script is already running."""
	if DEBUG: sys.stdout.write("Checking if already running other than my PID %d... " % os.getpid()); sys.stdout.flush()
	CMD = ["pgrep", "-f", __file__]
	if DEBUG: sys.stdout.write('Calling ' + ' '.join(CMD) + '\n'); sys.stdout.flush()
	process = Popen(CMD, stdout=PIPE)
	(output, err) = process.communicate()
	EXITCODE = process.wait()
	if DEBUG: sys.stdout.write("Check process exit code: %d\n" % EXITCODE); sys.stdout.flush()
	for line in output.decode('utf-8').split("\n"):
		if line == "": continue
		line = line.split()
		if DEBUG: sys.stdout.write("Found PID %d... " % int(line[0])); sys.stdout.flush()
		if int(line[0]) == 0: continue # Safety net
		if int(line[0]) == os.getpid():
			if DEBUG: sys.stdout.write("this is me, ignoring.\n"); sys.stdout.flush()
		else:
			if DEBUG: sys.stdout.write("stopping here as there is another instance running.\n"); sys.stdout.flush()
			sys.exit(0)

def calculate_pwm(PEAK_TEMP, MIN_TEMP, MAX_TEMP, MIN_FAN_PWM, MAX_FAN_PWM):
	# type: (float, int, int, int, int) -> int
	"""Calculate PWM percentage based on temperature thresholds."""
	PWMVAL = float(PEAK_TEMP)
	if   PWMVAL < MIN_TEMP: PWMVAL = MIN_TEMP # Sanitise input
	elif PWMVAL > MAX_TEMP: PWMVAL = MAX_TEMP # Sanitise input
	PWMVAL = (PWMVAL - MIN_TEMP) / (MAX_TEMP - MIN_TEMP) # Calculate ratio of where between min-max temps our value sits
	PWMVAL = MIN_FAN_PWM + ((MAX_FAN_PWM - MIN_FAN_PWM) * PWMVAL) # Calculate ratio between mix-max fan pwm
	if   PWMVAL < MIN_FAN_PWM: PWMVAL = MIN_FAN_PWM # Sanitise output
	elif PWMVAL > MAX_FAN_PWM: PWMVAL = MAX_FAN_PWM # Sanitise output
	return int(PWMVAL)

def parse_sdr_fields(line):
	# type: (object) -> list
	"""parse SDR fields from an IPMI response.
	- If necessary, parses string line parameter into a list
	- External IPMITOOL has a different 'sdr' output format than IPMICFG, so if necessary, swap SDR line field order
	"""
	global IPMITOOL
	if type(line) is list:
		l = line
	elif type(line) is str:
		if "|" not in line: return None
		l = [p.strip() for p in line.rstrip().split("|")]
	else:
		return None
	if len(l) < 3: return None
	if IPMITOOL:
		# ipmitool format: Name | Value | Status
		return [ l[2], l[0], l[1] ]
	# ipmicfg format: Status | Name | Value
	return l

def get_celsius_from_field(line):
	# type: (list) -> int
	"""Extracts Celsius temperature from an SDR field list.
	Returns integer Celsius value or None if not a valid temperature.
	"""
	if not line or len(line) < 3: return None
	val_str = line[2]

	# Standard format: 40C/104F
	match = re.search(r'(\d+)C\/', val_str)
	if match:
		return int(match.group(1))

	# Alternate format: 40 degrees C or 104 degrees F
	match = re.match(r'^(\d+) degrees (C|F)$', val_str)
	if match:
		val = int(match.group(1))
		if match.group(2) == 'F':
			return (val - 32) * 5 // 9
		return val

	return None

def config_test():
	# type: () -> int
	"""Test configuration file for validity.
	Return 0 if valid, 1 if invalid.
	"""
	global DEBUG
	global IPMITOOL
	global EXIT_ON_FAILURE
	global CONFIG_TEST
	CONFIG_TEST = True
	try:
		reload_config()
		EXIT_ON_FAILURE = True
		# Perform basic logical validation
		if POLL_RATE < 1: raise ValueError("Poll Rate (%d) is invalid; must be at least 1" % POLL_RATE)
		if IGNORE_TEMP_CHANGE_AMOUNT < 0: raise ValueError("Ignore Temp Change Amount (%d) is invalid; must be non-negative" % IGNORE_TEMP_CHANGE_AMOUNT)
		if AVERAGE_WINDOW < 1: raise ValueError("Temp Averaging Window (%d) is invalid; must be at least 1." % AVERAGE_WINDOW)

		if ZONE_A_MIN_TEMP >= ZONE_A_MAX_TEMP:
			raise ValueError("Zone A: 'Minimum Temperature Degrees' (%d) must be less than 'Maximum Temperature Degrees' (%d)"
					% (ZONE_A_MIN_TEMP, ZONE_A_MAX_TEMP))
		if ZONE_B_MIN_TEMP >= ZONE_B_MAX_TEMP:
			raise ValueError("Zone B: 'Minimum Temperature Degrees' (%d) must be less than 'Maximum Temperature Degrees' (%d)"
					% (ZONE_B_MIN_TEMP, ZONE_B_MAX_TEMP))
		if ZONE_A_MIN_FAN_PWM > ZONE_A_MAX_FAN_PWM:
			raise ValueError("Zone A: 'Minimum Temperature Fan PWM' (%d) cannot be greater than 'Maximum Temperature Fan PWM' (%d)"
					% (ZONE_A_MIN_FAN_PWM, ZONE_A_MAX_FAN_PWM))
		if ZONE_B_MIN_FAN_PWM > ZONE_B_MAX_FAN_PWM:
			raise ValueError("Zone B: 'Minimum Temperature Fan PWM' (%d) cannot be greater than 'Maximum Temperature Fan PWM' (%d)"
					% (ZONE_B_MIN_FAN_PWM, ZONE_B_MAX_FAN_PWM))

		for name, val in [
				("Zone A Min Fan PWM", ZONE_A_MIN_FAN_PWM),
				("Zone A Max Fan PWM", ZONE_A_MAX_FAN_PWM),
				("Zone B Min Fan PWM", ZONE_B_MIN_FAN_PWM),
				("Zone B Max Fan PWM", ZONE_B_MAX_FAN_PWM),
				]:
			if not (0 <= val <= 100):
				raise ValueError("%s (%d) is invalid; must be between 0 and 100" % (name, val))

		# ensure that we can get and parse output from the -sdr call
		sensorinfo = call_ipmi(["-sdr"])
		if sensorinfo[0] != 0:
			raise Exception("IPMI Communication Failure (Exit Code %d) using %s: %s" %
							(sensorinfo[0], IPMITOOL if IPMITOOL else "IPMICFG", str(sensorinfo[1]).strip()))
		found_a, found_b, found_valid_temp = False, False, False
		example_temp_field = False
		for line in sensorinfo[1].split("\n"):
			l = parse_sdr_fields(line)
			if not l: continue

			temp = get_celsius_from_field(l)
			if temp is not None:
				found_valid_temp = True
				if (ZONE_A_SENSOR_NAME_SEARCH.lower() in l[1].lower()) == ZONE_A_SENSOR_TEST_MATCH:
					found_a = True
				if (ZONE_B_SENSOR_NAME_SEARCH.lower() in l[1].lower()) == ZONE_B_SENSOR_TEST_MATCH:
					found_b = True
			elif not example_temp_field:
				# If parsing failed, check if it looks like a temp field to provide a helpful hint
				if re.match(r'\d+.*(C|F)|(C|F).*\d+', l[2]):
					example_temp_field = l[2]

		if not found_valid_temp:
			if example_temp_field:
				example_temp_field = "Possible temperature field example: " + example_temp_field
			else:
				example_temp_field = "No potential temperature fields found."
			raise ValueError("No temperature data found.  This system may use an unexpected data format.  " + example_temp_field)
		if not found_a:
			raise ValueError("No valid temperature sensors found for Zone A matching '%s'" % ZONE_A_SENSOR_NAME_SEARCH)
		if not found_b:
			raise ValueError("No valid temperature sensors found for Zone B matching '%s'" % ZONE_B_SENSOR_NAME_SEARCH)

		sys.stdout.write("Configuration is valid and sensors detected.\n")
		return 0
	except Exception as e:
		sys.stderr.write("Configuration error: " + str(e) + "\n")
		return 1

def set_fan_speed(zone, speed):
	# type: (int, float) -> bool
	"""Set the fan speed for a specific zone and return success status."""
	global USE_ALT_COMMANDS
	global EXIT_ON_FAILURE

	zoneName = "Zone A" if zone == 0 else "Zone B"

	sys.stdout.write('Setting our ' + zoneName + ' fan PWM to ' + str(speed) + '%... ')
	sys.stdout.flush()

	updatepwm = []

	if not USE_ALT_COMMANDS:
		zoneByte = hex(0x0 + zone);
		speedByte = hex(int((speed * 2.55) / 2))
		updatepwm = call_ipmi("-raw 0x30 0x70 0x66 0x01".split() + [zoneByte, speedByte])
		if updatepwm[0] != 0:
			sys.stdout.write("error setting fan PWM, attempting alternative command... ")
			sys.stdout.flush()
			USE_ALT_COMMANDS = True
			if EXIT_ON_FAILURE: sys_exit(updatepwm[0])

	if USE_ALT_COMMANDS:
		zoneByte = hex(0x10 + zone);
		speedByte = hex(int((speed * 2.55) / 1))
		updatepwm = call_ipmi("-raw 0x30 0x91 0x5A 0x3".split() + [zoneByte, speedByte])

	if updatepwm[0] != 0:
		sys.stdout.write("error setting fan PWM by alternative command too!\n")
		sys.stdout.flush()
		if EXIT_ON_FAILURE: sys_exit(updatepwm[0])

	# returns False on failure
	returnValue = True
	if len(updatepwm) < 1:
		returnValue = False
	else:
		returnValue = (updatepwm[0] == 0)

	if returnValue:
		sys.stdout.write("success!\n")
		sys.stdout.flush()
	return returnValue

def sys_exit(exitcode):
	# type: (int) -> None
	"""Reset fans to 100% (if configured) and terminate the script."""
	if RESTORE_FANS_ON_EXIT:
		sys.stdout.write('\nReceived exit signal. Resetting fans to 100% for safety...\n')
		set_fan_speed(0, 100)
		set_fan_speed(1, 100)
	sys.exit(exitcode)

def handle_signal(signum, frame):
	# type: (int, object) -> None
	"""Signal handler to ensure graceful termination."""
	sys_exit(0)

load_defaults(CONFIG_MAP)

# Validate configuration if requested
if "--configtest" in sys.argv:
	sys.exit(config_test())

# Process other command-line arguments
for arg in sys.argv:
	# Determine if we should use terse log output mode and its level
	if arg.startswith("--terse-output="):
		TERSE_OUTPUT = int(arg.split("=", 1)[1])
		break # Option found, no need to check further
	elif arg == "--terse-output":
		TERSE_OUTPUT = 1
		break # Option found, no need to check further

signal.signal(signal.SIGTERM, handle_signal)
signal.signal(signal.SIGINT, handle_signal)

# Main program loop starts here
reload_config(); check_if_already_running();
ZONE_A_TEMP_SAMPLES = [ZONE_A_MAX_TEMP]
ZONE_A_LAST_PWM = 0
ZONE_B_TEMP_SAMPLES = [ZONE_B_MAX_TEMP]
ZONE_B_LAST_PWM = 0
USE_ALT_COMMANDS=True
LAST_OUTPUT_LINE=""
while True:
	# Reset variables
	PEAK_ZONE_A_TEMP = -999
	FINAL_ZONE_A_TEMP = 0
	PEAK_ZONE_B_TEMP = -999
	FINAL_ZONE_B_TEMP = 0
	ZONE_A_FINAL_PWM = 0
	ZONE_B_FINAL_PWM = 0
	FAILED_FAN = False
	reload_config()

	if not TERSE_OUTPUT:
		# Print time
		sys.stdout.write('\nTimestamp of run: ' + time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()) + ' UTC\n=========================================\n'); sys.stdout.flush()

	# Get sensor values from IPMI
	sensorinfo = call_ipmi(["-sdr"])
	if sensorinfo[0] != 0:
		sys.stdout.write("Error getting info from IPMI: " + sensorinfo[1] + "\n"); sys.stdout.flush()
		if EXIT_ON_FAILURE: sys_exit(sensorinfo[0])
		time.sleep(POLL_RATE)
		continue

	# Process our sensor values and grab the highest for each zone
	for line in sensorinfo[1].split("\n"):
		# Parse returned data if we can, otherwise ignore it
		line = parse_sdr_fields(line)
		if not line: continue
		if DEBUG: sys.stdout.write(line[1] + ": " + line[2] + "\n"); sys.stdout.flush()

		# Check to see if we have a failed fan
		if ((line[0].lower() == "fail") and ("fan" in line[1].lower())): FAILED_FAN = True

		temp = get_celsius_from_field(line)
		if temp is None: continue

		# Check to see if this sensor matches Zone A
		if (ZONE_A_SENSOR_NAME_SEARCH.lower() in line[1].lower()) == ZONE_A_SENSOR_TEST_MATCH:
			if DEBUG: sys.stdout.write("ZONE A SENSOR MATCH: " + line[1] + " " + str(temp) + "'C\n"); sys.stdout.flush()
			if temp > PEAK_ZONE_A_TEMP: PEAK_ZONE_A_TEMP = temp

		# Check to see if this sensor matches Zone B
		if (ZONE_B_SENSOR_NAME_SEARCH.lower() in line[1].lower()) == ZONE_B_SENSOR_TEST_MATCH:
			if DEBUG: sys.stdout.write("ZONE B SENSOR MATCH: " + line[1] + " "+ str(temp) + "'C\n"); sys.stdout.flush()
			if temp > PEAK_ZONE_B_TEMP: PEAK_ZONE_B_TEMP = temp

	# Handle the case where our search string didn't match any sensors
	if PEAK_ZONE_A_TEMP == -999:
		sys.stdout.write("No valid temperature sensors found for Zone A matching '%s'\n" % ZONE_A_SENSOR_NAME_SEARCH)
		PEAK_ZONE_A_TEMP = ZONE_A_MAX_TEMP
	if PEAK_ZONE_B_TEMP == -999:
		sys.stdout.write("No valid temperature sensors found for Zone B matching '%s'\n" % ZONE_B_SENSOR_NAME_SEARCH)
		PEAK_ZONE_B_TEMP = ZONE_B_MAX_TEMP

	# Average out temp values over the last AVERAGE_WINDOW samples to smooth RPM changes and output our values
	ZONE_A_TEMP_SAMPLES.append(PEAK_ZONE_A_TEMP)
	if len(ZONE_A_TEMP_SAMPLES) < AVERAGE_WINDOW:
		# if we have enough real samples, drop the default samples to stabilize fan speeds sooner
		if len(ZONE_A_TEMP_SAMPLES) == 3 and ZONE_A_TEMP_SAMPLES[0] == ZONE_A_MAX_TEMP:
			ZONE_A_TEMP_SAMPLES.pop(0)
	else:
		while len(ZONE_A_TEMP_SAMPLES) > AVERAGE_WINDOW: ZONE_A_TEMP_SAMPLES.pop(0)
	AVG_ZONE_A_TEMP = statistics.mean(ZONE_A_TEMP_SAMPLES)
	MAX_ZONE_A_TEMP = max(ZONE_A_TEMP_SAMPLES)
	FINAL_ZONE_A_TEMP = (MAX_ZONE_A_TEMP + AVG_ZONE_A_TEMP) / 2
	ZONE_B_TEMP_SAMPLES.append(PEAK_ZONE_B_TEMP)
	if len(ZONE_B_TEMP_SAMPLES) < AVERAGE_WINDOW:
		# if we have enough real samples, drop the default samples to stabilize fan speeds sooner
		if len(ZONE_B_TEMP_SAMPLES) == 3 and ZONE_B_TEMP_SAMPLES[0] == ZONE_B_MAX_TEMP:
			ZONE_B_TEMP_SAMPLES.pop(0)
	else:
		while len(ZONE_B_TEMP_SAMPLES) > AVERAGE_WINDOW: ZONE_B_TEMP_SAMPLES.pop(0)
	AVG_ZONE_B_TEMP = statistics.mean(ZONE_B_TEMP_SAMPLES)
	MAX_ZONE_B_TEMP = max(ZONE_B_TEMP_SAMPLES)
	FINAL_ZONE_B_TEMP = (MAX_ZONE_B_TEMP + AVG_ZONE_B_TEMP) / 2
	if not TERSE_OUTPUT:
		sys.stdout.write("\nMaximum Zone A temp = " + str(PEAK_ZONE_A_TEMP) + "'C, averaged " + str(int(AVG_ZONE_A_TEMP)) + "'C\nMaximum Zone B temp = " + str(PEAK_ZONE_B_TEMP) + "'C, averaged " + str(int(AVG_ZONE_B_TEMP)) + "'C\n"); sys.stdout.flush()
	else:
		output_line = ("Zone Temps (Now/Avg/Max): " +
			"A " + str(PEAK_ZONE_A_TEMP) + "'C " +
			"/ " + str(int(AVG_ZONE_A_TEMP)) + "'C " +
			"/ " + str(int(MAX_ZONE_A_TEMP)) + "'C; " +
			"B " + str(PEAK_ZONE_B_TEMP) + "'C " +
			"/ " + str(int(AVG_ZONE_B_TEMP)) + "'C " +
			"/ " + str(int(MAX_ZONE_B_TEMP)) + "'C\n")
		# Terseness level 2 only shows "now" temp when it is outside the avg-max range
		if (TERSE_OUTPUT >= 2) \
			and (int(AVG_ZONE_A_TEMP) <= PEAK_ZONE_A_TEMP <= MAX_ZONE_A_TEMP) \
			and (int(AVG_ZONE_B_TEMP) <= PEAK_ZONE_B_TEMP <= MAX_ZONE_B_TEMP):
			output_line = ("Zone Temps (Avg/Max): " +
				"A " + str(int(AVG_ZONE_A_TEMP)) + "'C " +
				"/ " + str(int(MAX_ZONE_A_TEMP)) + "'C; " +
				"B " + str(int(AVG_ZONE_B_TEMP)) + "'C " +
				"/ " + str(int(MAX_ZONE_B_TEMP)) + "'C\n")

		if output_line != LAST_OUTPUT_LINE:
			sys.stdout.write(output_line);
			sys.stdout.flush()
			LAST_OUTPUT_LINE = output_line

	# Calculate our fan PWM values
	if FAILED_FAN:
		sys.stdout.write('Failed fan detected. Setting both zones to 100% PWM!\n'); sys.stdout.flush()
		ZONE_A_TEMP_SAMPLES = [ZONE_A_MAX_TEMP]
		ZONE_B_TEMP_SAMPLES = [ZONE_B_MAX_TEMP]
		ZONE_A_FINAL_PWM = 100
		ZONE_B_FINAL_PWM = 100
	else:
		ZONE_A_FINAL_PWM = calculate_pwm(FINAL_ZONE_A_TEMP, ZONE_A_MIN_TEMP, ZONE_A_MAX_TEMP, ZONE_A_MIN_FAN_PWM, ZONE_A_MAX_FAN_PWM)
		ZONE_B_FINAL_PWM = calculate_pwm(FINAL_ZONE_B_TEMP, ZONE_B_MIN_TEMP, ZONE_B_MAX_TEMP, ZONE_B_MIN_FAN_PWM, ZONE_B_MAX_FAN_PWM)

	# Set fan speeds
	if abs(ZONE_A_FINAL_PWM - ZONE_A_LAST_PWM) > IGNORE_TEMP_CHANGE_AMOUNT:
		set_fan_speed(0, ZONE_A_FINAL_PWM)
		ZONE_A_LAST_PWM = ZONE_A_FINAL_PWM
	elif not TERSE_OUTPUT:
		sys.stdout.write('Not setting our Zone A fan PWM, little to no change since last time (' + str(ZONE_A_FINAL_PWM) + '%).\n'); sys.stdout.flush()

	if abs(ZONE_B_FINAL_PWM - ZONE_B_LAST_PWM) > IGNORE_TEMP_CHANGE_AMOUNT:
		set_fan_speed(1, ZONE_B_FINAL_PWM)
		ZONE_B_LAST_PWM = ZONE_B_FINAL_PWM
	elif not TERSE_OUTPUT:
		sys.stdout.write('Not setting our Zone B fan PWM, little to no change since last time (' + str(ZONE_B_FINAL_PWM) + '%).\n'); sys.stdout.flush()

	sys.stdout.flush()

	# Sleep 5 seconds
	time.sleep(POLL_RATE)