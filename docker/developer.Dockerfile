FROM docker:28-dind
RUN apk add --no-cache bash coreutils util-linux git openssh-client curl ca-certificates \
    python3 py3-pip nodejs npm build-base postgresql-client
COPY scripts/project-worktree.py /usr/local/bin/sbot-worktree
RUN chmod 755 /usr/local/bin/sbot-worktree
WORKDIR /workspace
