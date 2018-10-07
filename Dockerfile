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

COPY root/etc/apk/repositories /etc/apk/repositories
COPY --from=buildstage /usr/local/bin/mp4* /usr/local/bin/
COPY --from=buildstage /usr/local/lib/libmp4v2* /usr/local/lib/

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
    rm -rf /root/.cache /tmp/*

COPY root/ /

# /config - Holds the auto-transcoding config and the harness process logs (not the handbrake logs)
# /video_files - Holds the folder structures containing things to transcode, gets populated after running the first time
VOLUME ["/video_files", "/config"]
