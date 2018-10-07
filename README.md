# Unraid Video Transcode Service

This is based on work in these two repositories, and while the codebase, docker image, and approach
to processing video files has diverged quite a bit since then, it never would have existed without them.

* https://github.com/Valherun/transcoder
* https://github.com/andymccurdy/tested-transcoder

This is a docker instance intended to be used via SFTP/SMB/NFS or Unraid commandline to automatically
transcode video files in a variety of configurable ways using Don Melton's video Transcoding tools,
found at the link below:

https://github.com/donmelton/video_transcoding

The docker instance does _not_ include a GUI, it exists as a file processing service only.
To use it, you'll need to put files into the appropriate /video_files directory
(that path is within docker, you'll need to point it somewhere via unraid).

* /config
    - Holds the config YAML file and transcoder.log file only. You'll need to roll the log file yourself for now, if for some reason you're transcoding massive numbers of videos through the service.
* /video_files
    - The base directory for all your transcoding goodness. This is the base directory for everything referenced within the config.yaml file, I recommend reading (and tweaking) that config file as desired.
