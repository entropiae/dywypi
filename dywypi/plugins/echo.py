from dywypi.plugin_api import Plugin, command

class EchoPlugin(Plugin):
    name = 'echo'

    @command('echo')
    def do(self, args):
        return u' '.join(args)