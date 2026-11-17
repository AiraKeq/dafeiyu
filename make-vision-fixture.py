"""Create a non-private visual test image using only Python's standard library."""
from pathlib import Path
import struct
import zlib

ROOT=Path(__file__).resolve().parent
out=ROOT/'fixtures/vision-test.png'
out.parent.mkdir(parents=True,exist_ok=True)
width,height=640,360
rows=[bytearray([255]*(width*3)) for _ in range(height)]
def pixel(x,y,color):
    if 0<=x<width and 0<=y<height:
        rows[y][x*3:x*3+3]=bytes(color)
for y in range(40,190):
    span=(y-40)*75//150
    for x in range(105-span,106+span): pixel(x,y,(230,20,20))
for y in range(50,180):
    for x in range(245,375): pixel(x,y,(20,40,230))
for y in range(35,196):
    for x in range(415,576):
        if (x-495)**2+(y-115)**2<=75**2: pixel(x,y,(245,205,10))
font={'7':['11111','00001','00010','00100','01000','01000','01000'],
      '3':['11110','00001','00001','01110','00001','00001','11110'],
      '8':['01110','10001','10001','01110','10001','10001','01110'],
      '4':['00010','00110','01010','10010','11111','00010','00010']}
scale=9
for index,char in enumerate('7384'):
    for y,line in enumerate(font[char]):
        for x,value in enumerate(line):
            if value=='1':
                for dy in range(scale):
                    for dx in range(scale): pixel(180+index*65+x*scale+dx,250+y*scale+dy,(0,0,0))
def chunk(kind,data):
    return struct.pack('!I',len(data))+kind+data+struct.pack('!I',zlib.crc32(kind+data)&0xffffffff)
raw=b''.join(b'\0'+bytes(row) for row in rows)
png=b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('!IIBBBBB',width,height,8,2,0,0,0))+chunk(b'IDAT',zlib.compress(raw,9))+chunk(b'IEND',b'')
out.write_bytes(png)
print('Generated non-private vision fixture:',out.name,width,height)
