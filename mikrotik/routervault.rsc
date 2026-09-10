\
# RouterVault - MikroTik RouterOS v7 example
# CHANGE THESE VALUES:
:global RVDevice "MT-HQ-01"
:global RVHost "backup.example.com"
:global RVApi "https://backup.example.com"
:global RVApiKey "rv_CHANGE_ME"
:global RVSftpUser "backup"
:global RVSftpPass "CHANGE_ME"
:global RVSftpPort "2222"

:local stamp ([/system clock get date] . "-" . [/system clock get time])
:set stamp [:pick $stamp 0 10]

:local exportName ($RVDevice . "__" . $stamp . ".rsc")
:local backupName ($RVDevice . "__" . $stamp . ".backup")

/export show-sensitive=no file=$exportName
/system backup save name=$backupName dont-encrypt=yes

# Native SFTP upload. check-certificate applies to HTTPS fetch below.
# Adjust dst-path to match the SFTP container user's upload folder.
/tool fetch url=("sftp://" . $RVHost . ":" . $RVSftpPort . "/upload/" . $exportName) \
    user=$RVSftpUser password=$RVSftpPass src-path=$exportName upload=yes keep-result=no

/tool fetch url=("sftp://" . $RVHost . ":" . $RVSftpPort . "/upload/" . $backupName) \
    user=$RVSftpUser password=$RVSftpPass src-path=$backupName upload=yes keep-result=no

# Heartbeat over HTTPS
:local ros [/system package update get installed-version]
:local up [/system resource get uptime]
:local cpu [/system resource get cpu-load]
:local mem [/system resource get free-memory]
:local body ("{\"routeros_version\":\"" . $ros . "\",\"uptime\":\"" . $up . "\",\"cpu_load\":\"" . $cpu . "\",\"free_memory\":\"" . $mem . "\"}")

/tool fetch url=($RVApi . "/api/v1/device/heartbeat") \
    http-method=post \
    http-header-field=("Content-Type:application/json,X-Device-Key:" . $RVApiKey) \
    http-data=$body output=none check-certificate=yes

/file remove $exportName
/file remove $backupName
