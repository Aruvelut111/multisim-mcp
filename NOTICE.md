# NOTICE

This project is not affiliated with, endorsed by, or sponsored by
National Instruments / NI / Emerson. Multisim is a trademark of its
respective owner. A locally installed, licensed copy of Multisim 14+ is
required; this server only automates software you already own.

## No NI-derived design files in this repository

Donor designs (`data/donors/*.ms14`) and the blank base design
(`data/blank.ms14`) are **NI sample-derived artifacts**. They are not
covered by this repository's MIT license and are deliberately **not**
committed. Run the bootstrap tool to generate them locally from .ms14
files that exist on your machine (NI sample designs, your own saved
circuits, any design set you have the right to use):

```bat
python tools\bootstrap_donors.py --scan "C:\path\to\your\.ms14\designs"
```

`data/SOURCES.md` records exactly which local file each template came
from and stays on your disk only.
