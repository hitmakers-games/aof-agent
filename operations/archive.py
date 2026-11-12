"""Committed source snapshots and safe extraction for reproducible builds."""
import io
from pathlib import Path
import tarfile

from coding import git


INFRA_PATHS = ['modules', 'k8s/selectel', 'k8s/agent-telegram', 'k8s/agent-coding',
               'k8s/agent-database', 'k8s/agent-worker', 'k8s/agent-workspace']


def package(path, paths=None):
    arguments = ['archive', '--format=tar.gz', 'HEAD']
    if paths:
        arguments += ['--', *paths]
    result = git.run(arguments, path, binary=True)
    if len(result)>650*1024:
        raise ValueError('Source archive exceeds the ConfigMap limit; use an artifact store before growing this deployment.')
    return result


def unpack(source, destination):
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    stream = io.BytesIO(source) if isinstance(source, bytes) else source
    with tarfile.open(fileobj=stream, mode='r:gz') as archive:
        for member in archive.getmembers():
            path = destination/member.name
            if (not path.resolve().is_relative_to(destination.resolve()) or
                    not (member.isfile() or member.isdir())):
                raise ValueError('Source contains a link or a path outside its archive.')
        # The image uses Python 3.11. All members were checked above: no links,
        # devices, or escaping paths are passed to its older tar implementation.
        archive.extractall(destination)


if __name__=='__main__':
    import sys
    with open(sys.argv[1], 'rb') as source:
        unpack(source, sys.argv[2])
