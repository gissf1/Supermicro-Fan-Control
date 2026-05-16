#!/usr/bin/env python3

'''

Script to parse and process IPMI information from SuperMicro X8/9/10/11 boards and intelligently adjust fan PWM.
Written by JBG 20190715

*** PLEASE READ THE README FILE FOR USAGE INFORMATION ***

*** I TAKE NO RESPONSIBILITY FOR ANY DAMAGES THAT MAY OCCUR FROM USING THIS SCRIPT. NO WARRANTY WHATSOEVER ***

'''

# Import required modules
import os, sys, re, time, configparser, statistics, signal
from subprocess import Popen, PIPE
import shutil

# Set up our default variables with safe values
ZONE_A_SENSOR_NAME_SEARCH = r'^.*CPU.*$'
ZONE_A_SENSOR_TEST_MATCH = False
ZONE_A_MIN_TEMP = 50
ZONE_A_MIN_FAN_PWM = 80
ZONE_A_MAX_TEMP = 60
ZONE_A_MAX_FAN_PWM = 100
ZONE_B_SENSOR_NAME_SEARCH = r'^.*CPU.*$'
ZONE_B_SENSOR_TEST_MATCH = True
ZONE_B_MIN_TEMP = 50
ZONE_B_MIN_FAN_PWM = 80
ZONE_B_MAX_TEMP = 60
ZONE_B_MAX_FAN_PWM = 100
POLL_RATE = 5
IGNORE_TEMP_CHANGE_AMOUNT = 1
AVERAGE_WINDOW = 5
RESTORE_FANS_ON_EXIT = True
EXIT_ON_FAILURE = False
DEBUG = False
IPMITOOL = False
CONFIG_TEST = False
PREV_CONFIG_MTIME = None
TERSE_OUTPUT = False

# Wrapper for (re)reading config.ini
def reload_config():
	global DEBUG
	global IPMITOOL
	global CONFIG_TEST
	global PREV_CONFIG_MTIME

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

	global ZONE_A_SENSOR_NAME_SEARCH; ZONE_A_SENSOR_NAME_SEARCH = config.get('Fan Zone A', 'Sensor Name Search')
	global ZONE_A_SENSOR_TEST_MATCH;  ZONE_A_SENSOR_TEST_MATCH  = config.get('Fan Zone A', 'Sensor Test Match').lower() in ["yes", "true", "1"]
	global ZONE_A_MIN_TEMP;           ZONE_A_MIN_TEMP           = int(config.get('Fan Zone A', 'Minimum Temperature Degrees'))
	global ZONE_A_MIN_FAN_PWM;        ZONE_A_MIN_FAN_PWM        = int(config.get('Fan Zone A', 'Minimum Temperature Fan PWM'))
	global ZONE_A_MAX_TEMP;           ZONE_A_MAX_TEMP           = int(config.get('Fan Zone A', 'Maximum Temperature Degrees'))
	global ZONE_A_MAX_FAN_PWM;        ZONE_A_MAX_FAN_PWM        = int(config.get('Fan Zone A', 'Maximum Temperature Fan PWM'))

	global ZONE_B_SENSOR_NAME_SEARCH; ZONE_B_SENSOR_NAME_SEARCH = config.get('Fan Zone B', 'Sensor Name Search')
	global ZONE_B_SENSOR_TEST_MATCH;  ZONE_B_SENSOR_TEST_MATCH  = config.get('Fan Zone B', 'Sensor Test Match').lower() in ["yes", "true", "1"]
	global ZONE_B_MIN_TEMP;           ZONE_B_MIN_TEMP           = int(config.get('Fan Zone B', 'Minimum Temperature Degrees'))
	global ZONE_B_MIN_FAN_PWM;        ZONE_B_MIN_FAN_PWM        = int(config.get('Fan Zone B', 'Minimum Temperature Fan PWM'))
	global ZONE_B_MAX_TEMP;           ZONE_B_MAX_TEMP           = int(config.get('Fan Zone B', 'Maximum Temperature Degrees'))
	global ZONE_B_MAX_FAN_PWM;        ZONE_B_MAX_FAN_PWM        = int(config.get('Fan Zone B', 'Maximum Temperature Fan PWM'))

	global POLL_RATE;                 POLL_RATE                 = int(config.get('General Configuration', 'Poll Rate'))
	global IGNORE_TEMP_CHANGE_AMOUNT; IGNORE_TEMP_CHANGE_AMOUNT = int(config.get('General Configuration', 'Ignore Temp Change Amount'))
	global AVERAGE_WINDOW;            AVERAGE_WINDOW            = int(config.get('General Configuration', 'Temp Averaging Window', fallback="5"))
	global RESTORE_FANS_ON_EXIT;      RESTORE_FANS_ON_EXIT      = config.get('General Configuration', 'Restore Fans On Exit', fallback="True").lower() in ["yes", "true", "1"]
	global EXIT_ON_FAILURE;           EXIT_ON_FAILURE           = config.get('General Configuration', 'Exit On IPMI Failure').lower() in ["yes", "true", "1"]
	DEBUG = config.get('General Configuration', 'Debug Mode').lower() in ["yes", "true", "1"]

	# validate logic for safety
	if not CONFIG_TEST:
		if POLL_RATE < 1: POLL_RATE = 1
		if IGNORE_TEMP_CHANGE_AMOUNT < 0: IGNORE_TEMP_CHANGE_AMOUNT = 0
		if AVERAGE_WINDOW < 1: AVERAGE_WINDOW = 5

	global IPMITOOL;
	ipmitool_bin = None
	ipmitool_desc = None
	try:
		IPMITOOL = config.get('General Configuration', 'IPMITOOL')
		if IPMITOOL.lower() in [ "", "0", "false", "none" ]:
			IPMITOOL = False
		else:
			# validate the external command exists, or replace with False
			ipmitool_bin = shutil.which(IPMITOOL)
			if ipmitool_bin is None:
				err = "Unable to find ipmitool in system path: " + IPMITOOL
				if CONFIG_TEST:
					raise FileNotFoundError(err)
				if DEBUG:
					sys.stdout.write("\n\nError: " + err + "\n")
				IPMITOOL = False
	except configparser.NoOptionError as e:
		if DEBUG: sys.stdout.write("\n" + str(e) + "\n")
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
	return os.path.join(os.path.dirname(__file__), "./ipmitool/", "IPMICFG-Linux.x86")

# Wrapper for making IPMI calls
def call_ipmi(params):
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

# Wrapper for making sure we're not already running
def check_if_already_running():
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

# Wrapper for calculating fan PWM - this is quite complex
def calculate_pwm(PEAK_TEMP, MIN_TEMP, MAX_TEMP, MIN_FAN_PWM, MAX_FAN_PWM):
	PWMVAL = float(PEAK_TEMP)
	if   PWMVAL < MIN_TEMP: PWMVAL = MIN_TEMP # Sanitise input
	elif PWMVAL > MAX_TEMP: PWMVAL = MAX_TEMP # Sanitise input
	PWMVAL = (PWMVAL - MIN_TEMP) / (MAX_TEMP - MIN_TEMP) # Calculate ratio of where between min-max temps our value sits
	PWMVAL = MIN_FAN_PWM + ((MAX_FAN_PWM - MIN_FAN_PWM) * PWMVAL) # Calculate ratio between mix-max fan pwm
	if   PWMVAL < MIN_FAN_PWM: PWMVAL = MIN_FAN_PWM # Sanitise output
	elif PWMVAL > MAX_FAN_PWM: PWMVAL = MAX_FAN_PWM # Sanitise output
	return int(PWMVAL)

def parse_sdr_fields(line):
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
	""" sets the fan speed and returns True on success; False on failure. """
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
	"""Reset fans to 100% and exit gracefully."""
	if RESTORE_FANS_ON_EXIT:
		sys.stdout.write('\nReceived exit signal. Resetting fans to 100% for safety...\n')
		set_fan_speed(0, 100)
		set_fan_speed(1, 100)
	sys.exit(exitcode)

def handle_signal(signum, frame):
	# type: (int, object) -> None
	"""Exit gracefully."""
	sys_exit(0)

# Validate configuration if requested
if "--configtest" in sys.argv:
	sys.exit(config_test())

# determine if we should use terse log output mode
if "--terse-output" in sys.argv:
	TERSE_OUTPUT = True

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
	PEAK_ZONE_A_TEMP = 0
	FINAL_ZONE_A_TEMP = 0
	PEAK_ZONE_B_TEMP = 0
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