# Both build arguments must be explicit, unique local aliases verified against
# immutable image IDs by the build caller. Use --pull=false --network=none.
# Offline local Git only; .git is excluded from normal workspace snapshots.
ARG OMNI_GIT_SOURCE
ARG OMNI_TOOL_BASE
FROM ${OMNI_GIT_SOURCE} AS git_source
FROM ${OMNI_TOOL_BASE}
USER root
COPY --from=git_source /usr/bin/git /usr/bin/git
COPY --from=git_source /usr/lib/git-core/ /usr/lib/git-core/
COPY --from=git_source /usr/share/git-core/ /usr/share/git-core/
COPY --from=git_source /usr/share/perl5/Git.pm /usr/share/perl5/Git.pm
COPY --from=git_source /usr/share/perl5/Git/ /usr/share/perl5/Git/
COPY --from=git_source /usr/share/doc/git/copyright /usr/share/doc/git/copyright
USER 0:0
