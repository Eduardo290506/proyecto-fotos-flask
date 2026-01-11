from jinja2 import Environment, FileSystemLoader, TemplateSyntaxError
import sys

TPL_DIR = r"c:\Users\Eduardo Santos\Desktop\foto_manager\templates"
TPL_NAME = 'admin.html'

env = Environment(loader=FileSystemLoader(TPL_DIR))
try:
    src = env.loader.get_source(env, TPL_NAME)[0]
    env.parse(src)
    print('TEMPLATE_OK')
except TemplateSyntaxError as e:
    print('TEMPLATE_ERROR')
    print(e)
    sys.exit(2)
except Exception as e:
    print('OTHER_ERROR')
    print(e)
    sys.exit(3)
