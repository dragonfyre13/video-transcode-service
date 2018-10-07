############## build stage ##############
FROM lsiobase/alpine:3.8 as buildstage

COPY build/ /
WORKDIR /src
RUN echo "**** build mp4v2 (for mp4track) from source since no alpine package exists ****" && \
    apk add --no-cache --virtual=build-dependencies g++ make && \
    tar -xjf mp4v2-2.0.0.tar.bz2
WORKDIR /src/mp4v2-2.0.0
RUN ./configure --disable-debug && make && make install

############## runtime stage ##############
FROM lsiobase/alpine:3.8

LABEL maintainer="Dragonfyre13"
ENV PYTHONIOENCODING="UTF-8"

COPY root/ /
COPY --from=buildstage /usr/local/bin/mp4* /usr/local/bin/
COPY --from=buildstage /usr/local/lib/libmp4v2* /usr/local/lib/
# Now included in root/etc/apk/repositories
## echo "**** Add edge repositories for alpine to use handbrake/etc. ****"
## echo "@edge http://nl.alpinelinux.org/alpine/edge/main" >> /etc/apk/repositories
## echo "@edgecommunity http://nl.alpinelinux.org/alpine/edge/community" >> /etc/apk/repositories
## echo "@edgetesting http://nl.alpinelinux.org/alpine/edge/testing" >> /etc/apk/repositories

# Required for runtime of mp4track: libgcc libstdc++
RUN echo "**** install supporting packages ****" && \
    apk add --no-cache \
        libgcc libstdc++ \
        python2 py2-pip ruby ruby-rdoc \
        handbrake@edgetesting x265-libs@edgecommunity \
        ffmpeg mkvtoolnix && \
    echo "**** install video_transcoding gem ****" && \
    gem install video_transcoding && \
    echo "**** install latest pip version ****" && \
    pip install --no-cache-dir -U pip && \
    echo "**** install other pip packages ****" && \
    pip install --no-cache-dir -U pyyaml && \
    echo "**** Clean up temporary files ****" && \
    rm -rf /root/.cache /tmp/* && \
    chmod +x /usr/bin/transcoder.py

# Now we need to setup access to required folders
# /config
#   - Holds the auto-transcoding config
#   - Holds the harness process' logs (not the handbrake logs)
#
# /video_files
#   - Holds the folder structures containing things to transcode
#   - Gets populated after running with a given config

VOLUME ["/video_files", "/config"]

CMD ["/usr/bin/transcoder.py"]
