#!/usr/bin/env python3
from jinja2 import Template
import glob, os
for f in glob.glob('sonic-buildimage/src/sonic-yang-models/yang-templates/*.yang.j2'):
    t = Template(open(f).read())
    outpath = 'yang-models/' + os.path.basename(f).replace('.j2', '')
    open(outpath, 'w').write(t.render(yang_model_type='py'))
    print(f'Rendered {outpath}')
