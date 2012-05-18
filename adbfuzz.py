#!/usr/bin/env python
# ***** BEGIN LICENSE BLOCK *****
# Version: MPL 2.0
#
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
#
# The Original Code is ADBFuzz.
#
# The Initial Developer of the Original Code is Christian Holler (decoder).
#
# Contributors:
#  Christian Holler <decoder@mozilla.com> (Original Developer)
#
# ***** END LICENSE BLOCK *****

import subprocess
import time
import shutil
import os
import signal
import sys
import traceback
import random
import uuid
from ConfigParser import SafeConfigParser

from mozdevice import DeviceManagerADB

from adbfuzzconfig import ADBFuzzConfig
from triage import Triager
from minidump import Minidump
from logfilter import LogFilter

def usage():
  print ""
  print "Usage: " + sys.argv[0] + " cfgFile cmd params"
  print "  Supported commands:"
  print "    run        - Run the fuzzer until manually aborted"
  print "    deploy     - Deploy the given Firefox package and prefs"
  print "      param[0]: path to package file"
  print "      param[1]: path to prefs file"
  print "    showdump   - Show the (symbolized) trace for a given dump"
  print "      param[0]: path to dump file"
  print "      param[1]: symbol search path"
  print "    reproduce"
  print "      param[0]: file to test"
  print "      param[1]: run timeout"
  print "      param[2]: crash type (crash or abort)"
  print ""
def main():
  if (len(sys.argv) > 2):
      cfgFile = sys.argv.pop(1)
      fuzzInst = ADBFuzz(cfgFile)
  else:
      print "Missing configuration file!"
      usage()
      exit(1)
  
  cmd = sys.argv.pop(1)
  if (cmd == "showdump"):
    print "Obtaining symbolized trace..."
    dumpFile = sys.argv[1]
    libSearchPath = sys.argv[2]
    minidump = Minidump(dumpFile, libSearchPath)
    symbolTrace = minidump.getSymbolizedCrashTrace()
    print ""
    for frame in symbolTrace:
      print "#" + frame[0] + "\t" + frame[1] + " at " + frame[2]
  elif (cmd == "reproduce"):
    fuzzInst.config.fuzzerFile = sys.argv[1]
    fuzzInst.config.runTimeout = int(sys.argv[2])
    
    isCrash = True
    
    if sys.argv[3] == 'crash':
      isCrash = True
    elif sys.argv[3] == 'abort':
      isCrash = False
    else:
      raise Exception("Unknown crash type " + sys.argv[3] + " specified!")
    
    if fuzzInst.testCrash(isCrash):
      exit(0)
    exit(1)
  elif (cmd == "run"):
    fuzzInst.remoteInit()
    fuzzInst.loopFuzz()
  elif (cmd == "deploy"):
    fuzzInst.deploy(sys.argv[1], sys.argv[2])
  elif (cmd == "reset"):
    fuzzInst.reset(sys.argv[1])

class ADBFuzz:

  def __init__(self, cfgFile):
    self.config = ADBFuzzConfig(cfgFile)

    self.HTTPProcess = None
    self.logProcesses = []
    self.logThreads = []
    self.remoteInitialized = None

    self.triager = Triager(self.config)
    
    # Seed RNG with localtime
    random.seed()
    
  def deploy(self, packageFile, prefFile):
    self.dm = DeviceManagerADB(self.config.remoteAddr, 5555)
    
    # Install a signal handler that shuts down our external programs on SIGINT
    signal.signal(signal.SIGINT, self.signal_handler)
    
    self.dm.updateApp(packageFile)
    
    # Standard init stuff
    self.appName = self.dm.packageName
    self.appRoot = self.dm.getAppRoot(self.appName)
    self.profileBase = self.appRoot + "/files/mozilla"
    
    # Start Fennec, so a profile is created if this is the first install
    self.startFennec()
    
    # Grant some time to create profile
    time.sleep(self.config.runTimeout)
    
    # Stop Fennec again
    self.stopFennec()
    
    # Now try to get the profile(s)
    self.profiles = self.getProfiles()

    if (len(self.profiles) == 0):
      print "Failed to detect any valid profile, aborting..."
      return 1

    self.defaultProfile = self.profiles[0]

    if (len(self.profiles) > 1):
      print "Multiple profiles detected, using the first: " + self.defaultProfile
      
    # Push prefs.js to profile
    self.dm.pushFile(prefFile, self.profileBase + "/" + self.defaultProfile + "/prefs.js")
    
    print "Successfully deployed package."
    
  def reset(self, prefFile):
    self.dm = DeviceManagerADB(self.config.remoteAddr, 5555)
    
    # Install a signal handler that shuts down our external programs on SIGINT
    signal.signal(signal.SIGINT, self.signal_handler)
    
    # Standard init stuff
    self.appName = self.dm.packageName
    self.appRoot = self.dm.getAppRoot(self.appName)
    self.profileBase = self.appRoot + "/files/mozilla"
    
    # Now try to get the old profile(s)
    self.profiles = self.getProfiles()
    
    for profile in self.profiles:
      self.dm.removeDir(self.profileBase + "/" + profile)
    
    # Start Fennec, so a new profile is created
    self.startFennec()
    
    # Grant some time to create profile
    time.sleep(self.config.runTimeout)
    
    # Stop Fennec again
    self.stopFennec()
    
    # Now try to get the profile(s) again
    self.profiles = self.getProfiles()

    if (len(self.profiles) == 0):
      print "Failed to detect any valid profile, aborting..."
      return 1

    self.defaultProfile = self.profiles[0]

    if (len(self.profiles) > 1):
      print "Multiple profiles detected, using the first: " + self.defaultProfile
      
    # Push prefs.js to profile
    self.dm.pushFile(prefFile, self.profileBase + "/" + self.defaultProfile + "/prefs.js")
    
    print "Successfully resetted profile."

  def remoteInit(self):
    if (self.remoteInitialized != None):
      return

    self.dm = DeviceManagerADB(self.config.remoteAddr, 5555)
    self.appName = self.dm.packageName
    self.appRoot = self.dm.getAppRoot(self.appName)
    self.profileBase = self.appRoot + "/files/mozilla"
    self.profiles = self.getProfiles()

    # Install a signal handler that shuts down our external programs on SIGINT
    signal.signal(signal.SIGINT, self.signal_handler)

    if (len(self.profiles) == 0):
      print "Failed to detect any valid profile, aborting..."
      return 1

    self.defaultProfile = self.profiles[0]

    if (len(self.profiles) > 1):
      print "Multiple profiles detected, using the first: " + self.defaultProfile
      
    
    # Workaround for bug 754575. Avoid using DeviceManagerADB's "removeDir" because
    # that calls "rm" on every single entry which takes a lot of additional time.
    print "Purging possible cache leftover directories..."
    self.dm.runCmd(['shell', 'rm', '-r', self.profileBase + "/" + self.defaultProfile + "/Cache.Trash*"]).communicate()

    self.remoteInitialized = True

  def signal_handler(self, signal, frame):
    self.cleanupProcesses()
    sys.exit(0)

  def cleanupProcesses(self):
    self.stopFennec()
    if (self.HTTPProcess != None):
      try:
        self.HTTPProcess.terminate()
      except:
        pass
    if (self.logProcesses != None):
      try:
        self.stopLoggers()
      except:
        pass

  def loopFuzz(self):
    try:
      while True:
        self.runFuzzer()
    except:
      self.cleanupProcesses()
      raise

  def runFuzzer(self):
    self.remoteInit()

    # Ensure Fennec isn't running
    if self.isFennecRunning():
      self.stopFennec()

    # Clean all existing minidumps
    if not self.clearMinidumps():
      raise Exception("Failed to clean existing minidumps")

    # Start our HTTP server for serving the fuzzer code
    self.HTTPProcess = self.startHTTPServer()

    # Start all loggers
    self.startLoggers()

    # Start Fennec
    self.startFennec()

    # Even though the program is already running, we should grant it
    # some extra time to load the fuzzer source and start running,
    # so it isn't directly diagnosed as hanging
    time.sleep(10);
    
    logSize = 0
    hangDetected = False
    forceRestart = False
    while(self.isFennecRunning() and not self.checkLoggingThreads()):
      time.sleep(self.config.runTimeout)

      if not os.path.exists(self.logFile):
        raise Exception("Logfile not present. If you are using websockets, this could indicate a network problem.")

      # Poor man's hang detection. Yes, this is a bad
      # idea, just for the sake of proof-of-concept
      newLogSize = os.path.getsize(self.logFile)
      if (logSize == newLogSize):
        hangDetected = True
        break
      else:
        logSize = newLogSize
        if newLogSize > self.config.maxLogSize:
          forceRestart = True
          break

    if hangDetected or forceRestart:
      self.stopFennec()
      self.stopLoggers()
      print "Hang detected or running too long, restarting..."
    else:
      try:
        # Fennec died or a logger found something
        checkCrashDump = True
        crashUUID = None
        minidump = None
        
        # If Fennec is still running, stop it now
        if self.isFennecRunning():
          checkCrashDump = False
          self.stopFennec()
        
        # Terminate our logging processes first
        self.stopLoggers()
        
        if checkCrashDump:
          dumps = self.getMinidumps()
          if (len(dumps) > 1):
            raise Exception("Multiple dumps detected!")
            
          if (len(dumps) < 1):
            raise Exception("No crash dump detected!")
    
          if not self.fetchMinidump(dumps[0]):
            raise Exception("Failed to fetch minidump with UUID " + dumps[0])
  
          crashUUID = dumps[0]
  
          # Copy logfiles
          shutil.copy2(self.syslogFile, dumps[0] + ".syslog")
          shutil.copy2(self.logFile, dumps[0] + ".log")
    
          minidump = Minidump(dumps[0] + ".dmp", self.config.libDir)
        else:
          # We need to generate an arbitrary ID here
          crashUUID = str(uuid.uuid4())
          
          # Copy logfiles
          shutil.copy2(self.syslogFile, crashUUID + ".syslog")
          shutil.copy2(self.logFile, crashUUID + ".log")
  
        print "Crash detected. Reproduction logfile stored at: " + crashUUID + ".log"
        if checkCrashDump:
          crashTrace = minidump.getCrashTrace()
          crashType = minidump.getCrashType()
          print "Crash type: " + crashType
          print "Crash backtrace:"
          print ""
          print crashTrace
        else:
          print "Crash type: Abnormal behavior (e.g. Assertion violation)"
  
        self.triager.process(crashUUID, minidump, crashUUID + ".syslog", crashUUID + ".log")
      except Exception, e:
        print "Error during crash processing: "
        print traceback.format_exc()

    self.HTTPProcess.terminate()
    return

  def testCrash(self, isCrash):
    self.remoteInit()

    # Ensure Fennec isn't running
    if self.isFennecRunning():
      self.stopFennec()

    # Clean all existing minidumps
    if not self.clearMinidumps():
      raise Exception("Failed to clean existing minidumps")

    # Start our HTTP server for serving the fuzzer code
    self.HTTPProcess = self.startHTTPServer()
    
    # Start a logcat instance that goes to stdout for progress monitoring
    logProcess = self.startNewDeviceLog(toStdout=True)

    # Start Fennec
    self.startFennec()

    startTime = time.time()

    while(self.isFennecRunning() and not self.checkLoggingThreads()):
      time.sleep(1)
      if ((time.time() - startTime) > self.config.runTimeout):
        self.stopFennec()
        self.HTTPProcess.terminate()
        self.stopLoggers()
        print "[TIMEOUT]"
        return False
    
    abortedByLogThread = False
    if self.isFennecRunning():
      abortedByLogThread = True
      self.stopFennec()

    self.HTTPProcess.terminate()
    self.stopLoggers()

    # Fennec died, check for crashdumps
    dumps = self.getMinidumps()
    if (len(dumps) > 0):
      if isCrash:
        print "[Crash reproduced successfully]"
        return True
      else:
        print "[Crashed while testing an abort]"
        return False
    elif abortedByLogThread:
      if isCrash:
        print "[Aborted while testing a crash]"
        return False
      else:
        print "[Abort reproduced successfully]"
        return True
    else:
      # Fennec exited, but no crash/abort
      print "[Exit without Crash]"
      return False

  def getProfiles(self):
    profiles = []

    candidates = self.dm.listFiles(self.profileBase)
    for candidate in candidates:
      if self.dm.dirExists(self.profileBase + "/" + candidate + "/minidumps"):
        profiles.append(candidate)

    return profiles

  def getMinidumps(self):
    dumps = self.dm.listFiles(self.profileBase + "/" + self.defaultProfile + "/minidumps")
    dumpIDs = []

    for dump in dumps:
      if dump.find('.dmp') > -1:
        dumpIDs.append(dump.replace('.dmp',''))

    return dumpIDs

  def fetchMinidump(self, dumpId):
    dumpPath = self.profileBase + "/" + self.defaultProfile + "/minidumps/" + dumpId + ".dmp"
    print dumpPath
    if (self.dm.getFile(dumpPath, dumpId + ".dmp") != None):
      return True

    return False;

  def clearMinidump(self, dumpId):
    dumpPath = self.profileBase + "/" + self.defaultProfile + "/minidumps/" + dumpId + ".dmp"
    extraPath = self.profileBase + "/" + self.defaultProfile + "/minidumps/" + dumpId + ".extra"
    
    if (self.dm.removeFile(dumpPath) != None and self.dm.removeFile(extraPath) != None):
      return True

    return False;

  def clearMinidumps(self):
    dumps = self.getMinidumps()
    for dump in dumps:
      if not self.clearMinidump(dump):
        return False

    return True

  def startLoggers(self):
    if self.config.useWebSockets:
      # This method starts itself multiple processes (proxy included)
      self.startNewWebSocketLog()
    self.startNewDeviceLog()
    
  def stopLoggers(self):
    # Terminate our logging processes
    while (len(self.logProcesses) > 0):
      self.logProcesses.pop().terminate()
      
    while (len(self.logThreads) > 0):
      logThread = self.logThreads.pop()
      logThread.terminate()
      logThread.join()
      
  def checkLoggingThreads(self):
    for logThread in self.logThreads:
      # Check if the thread aborted but did not hit EOF.
      # That means we hit something interesting.
      if not logThread.isAlive() and not logThread.eof:
        return True
    
    return False

  def startNewDeviceLog(self, toStdout=False):
    # Clear the log first
    subprocess.check_call(["adb", "logcat", "-c"])
    
    logCmd = ["adb", "logcat", "-s", "Gecko:v", "GeckoDump:v", "GeckoConsole:v", "MOZ_Assert:v"]

    if toStdout:
      logFile = None
    else:
      # Logfile
      self.syslogFile = 'device.log'
      logFile = self.syslogFile
      
    # We start the logProcess here so we can terminate it before joining the thread
    logProcess = subprocess.Popen(logCmd, shell=False, stdout=subprocess.PIPE, stderr=None)
  
    # Start logging thread
    logThread = LogFilter(self.config, self.triager, logProcess, logFile)
    logThread.start()
    
    self.logThreads.append(logThread)
    self.logProcesses.append(logProcess)

  def startNewWebSocketLog(self):
    self.logFile = 'websock.log'
    logProcess = subprocess.Popen(["em-websocket-proxy", "-p", self.config.localListenPort, "-q", self.config.localWebSocketPort, "-r", "localhost"])
    self.logProcesses.append(logProcess)
    proxyProcess = subprocess.Popen(["python", "websocklog.py", "localhost", self.config.localWebSocketPort])
    self.logProcesses.append(proxyProcess)

  def startHTTPServer(self):
    HTTPProcess = subprocess.Popen(["python", "-m", "SimpleHTTPServer", self.config.localPort ])
    return HTTPProcess

  def startFennec(self):
    env = {}
    env['MOZ_CRASHREPORTER_NO_REPORT'] = '1'
    env['MOZ_CRASHREPORTER_SHUTDOWN'] = '1'
    self.dm.launchProcess([self.appName, "http://" + self.config.localAddr + ":" + self.config.localPort + "/" + self.getSeededFuzzerFile()], None, None, env)

  def stopFennec(self):
    ret = self.dm.killProcess(self.appName, True)

    if self.isFennecRunning():
      # Try sleeping first and give the process time to react
      time.sleep(5)
      if self.isFennecRunning():
        # If the process doesn't terminate, try SIGKILL
        print "Process did not react to SIGTERM, trying SIGKILL"
        return self.dm.killProcess(self.appName, True)

    return ret

  def isFennecRunning(self):
    procList = self.dm.getProcessList()
    for proc in procList:
      if (proc[1] == self.appName):
        return True

    return False
  
  def getSeededFuzzerFile(self):
    fuzzerFile = self.config.fuzzerFile
    fuzzerSeed = random.randint(0,2**32-1)
    return fuzzerFile.replace('#SEED#', str(fuzzerSeed))

if __name__ == "__main__":
  main()
