import os, string, sys, shutil, glob, re, tarfile, logging, argparse
from datetime import datetime, timedelta

from subprocess import *
from math import *
from xml.etree import cElementTree as ET

from lib import mosaic, utils
import numpy
import gdal, ogr,osr, gdalconst
    
logger = logging.getLogger("logger")
logger.setLevel(logging.DEBUG)

gdal.SetConfigOption('GDAL_PAM_ENABLED','NO')


    
def main():
    
    #########################################################
    ####  Handle args
    #########################################################

    #### Set Up Arguments 
    parent_parser = mosaic.buildMosaicParentArgumentParser()
    parser = argparse.ArgumentParser(
        parents=[parent_parser],
        description="Create mosaic subtile"
	)
    
    parser.add_argument("tile", help="output tile name")
    parser.add_argument("src", help="textfile of input rasters (tif only)")
    
    parser.add_argument("--wd",
                        help="scratch space (default is mosaic directory)")
    parser.add_argument("--gtiff-compression", choices=mosaic.GTIFF_COMPRESSIONS, default="lzw",
                        help="GTiff compression type. Default=lzw (%s)"%string.join(mosaic.GTIFF_COMPRESSIONS,','))
    
    #### Parse Arguments
    args = parser.parse_args()

    status = 0
        
    bands = args.bands
    inpath = args.src
    tile = args.tile
    ref_xres, ref_yres = args.resolution
    xmin,xmax,ymin,ymax = args.extent
    dims = "-tr %s %s -te %s %s %s %s" %(ref_xres,ref_yres,xmin,ymin,xmax,ymax)
    
    ##### Configure Logger
    logfile = os.path.splitext(tile)[0]+".log"
    lfh = logging.FileHandler(logfile)
    lfh.setLevel(logging.DEBUG)
    formatter = logging.Formatter('%(asctime)s %(levelname)s- %(message)s','%m-%d-%Y %H:%M:%S')
    lfh.setFormatter(formatter)
    logger.addHandler(lfh) 
    
    #### get working directory
    if args.wd:
        if os.path.isdir(args.wd):
            localpath = args.wd
        else:
            parser.error("scratch space directory does not exist: {0}".format(args.wd))
    else:
        localpath = os.path.dirname(tile)
    
    intersects = []
    if os.path.isfile(inpath):
        t = open(inpath,'r')
        for line in t.readlines():
            intersects.append(line.rstrip('\n').rstrip('\r'))
        t.close()
    else:
        logger.error("Intersecting image file does not exist: {}".format(inpath))

    logger.info(tile)

    logger.info("Number of image found in source file: {}".format(len(intersects)))
    
    wd = os.path.join(localpath,os.path.splitext(os.path.basename(tile))[0])
    if not os.path.isdir(wd):
        os.makedirs(wd)
    localtile2 = os.path.join(wd,os.path.basename(tile)) 
    localtile1 = localtile2.replace(".tif","_temp.tif")
    
    del_images = []
    final_intersects = []
    images = {}
    
    for image in intersects:
        ds = gdal.Open(image)
        if ds is not None:
            srcbands = ds.RasterCount
            srcnodata_val = ds.GetRasterBand(1).GetNoDataValue()
            images[image] = (srcbands, srcnodata_val)
            final_intersects.append(image)
            logger.info("%s" %(os.path.basename(image)))
        else:
            logger.error("Cannot open image: {}".format(image))
    
        ds = None
    
    logger.info("Number of images: %i" %(len(final_intersects)))
    
    
    #### Get Extent geometry 
    poly_wkt = 'POLYGON (( %s %s, %s %s, %s %s, %s %s, %s %s ))' %(xmin,ymin,xmin,ymax,xmax,ymax,xmax,ymin,xmin,ymin)
    tile_geom = ogr.CreateGeometryFromWkt(poly_wkt)
    
    c = 0
    for img in final_intersects:
            
        #### Check if bands number is correct
        mergefile = img
        srcbands, srcnodata_val = images[img]

        if args.force_pan_to_multi is True and bands > 1:
            if srcbands == 1:
                mergefile = os.path.join(wd,os.path.basename(img)[:-4])+"_merge.tif"
                cmd = 'gdal_merge.py -ps %s %s -separate -o "%s" "%s"' %(ref_xres, ref_yres, mergefile, string.join(([img] * bands),'" "'))
                utils.exec_cmd(cmd)
        srcnodata = string.join(([str(srcnodata_val)] * bands)," ")

        if args.median_remove is True:
            src = mergefile
            dst = os.path.join(wd,os.path.basename(mergefile)[:-4])+"_median_removed.tif"
            status = BandSubtractMedian(src,dst)
            if status == 1:
                logger.error("BandSubtractMedian() failed on {}".format(mergefile))
                sys.exit(1)
            ds = gdal.Open(dst)
            if ds is not None:
                srcnodata_val = ds.GetRasterBand(1).GetNoDataValue()
                srcnodata = string.join(([str(srcnodata_val)] * bands)," ")
                mergefile = dst
            else:
                logger.error("BandSubtractMedian() failed at gdal.Open({})".format(dst))
                sys.exit(1)
            
        if c == 0:
            if os.path.isfile(localtile1):
                logger.info("localtile1 already exists")
                status = 1
                break
            cmd = 'gdalwarp %s -srcnodata "%s" -dstnodata "%s" "%s" "%s"' %(dims,srcnodata,srcnodata,mergefile,localtile1)
            utils.exec_cmd(cmd)
            
        else:
            cmd = 'gdalwarp -srcnodata "%s" "%s" "%s"' %(srcnodata,mergefile,localtile1)
            utils.exec_cmd(cmd)
            
        c += 1
       
        if not mergefile == img:
            del_images.append(mergefile)
            
    del_images.append(localtile1)        
    
    if status == 0:
        ####  Write to Compressed file
        if os.path.isfile(localtile1):
            if args.gtiff_compression == 'lzw':
                compress_option = '-co "compress=lzw"'
            elif args.gtiff_compression == 'jpeg95':
                compress_option =  '-co "compress=jpeg" -co "jpeg_quality=95"'
                
            cmd = 'gdal_translate -stats -of GTiff %s -co "PHOTOMETRIC=MINISBLACK" -co "TILED=YES" -co "BIGTIFF=IF_SAFER" "%s" "%s"' %(compress_option,localtile1,localtile2)
            utils.exec_cmd(cmd)
        
        ####  Build Pyramids        
        if os.path.isfile(localtile2):
            cmd = 'gdaladdo "%s" 2 4 8 16 30' %(localtile2)
            utils.exec_cmd(cmd)
        
        #### Copy tile to destination
        if os.path.isfile(localtile2):
            logger.info("Copying output files to destination dir")
            mosaic.copyall(localtile2,os.path.dirname(tile))
            
        del_images.append(localtile2)
    
    
    #### Delete temp files
    utils.delete_temp_files(del_images)
    shutil.rmtree(wd)
   
    logger.info("Done")


def BandSubtractMedian(srcfp,dstfp):
    # Subtract the median from each band of srcfp and write the result
    # to dstfp.
    # Band types byte, uint16 and int16 will be output as int16 with nodata -32768.
    # Band types uint32 and int32 will be output as int32 with nodata -2147483648.

    if os.path.isfile(srcfp):
        ds = gdal.Open(srcfp)
        if ds:
            datatype = ds.GetRasterBand(1).DataType
            if not (datatype in [1,2,3,4,5]):
                logger.error("BandSubtractMedian only works on integer data types")
                return 1
            elif (datatype in [1,2,3]):
                out_datatype = 3
                out_nodataval = -32768
                out_min = -32767
            else:
                out_datatype = 5
                out_nodataval = -2147483648
                out_min = -2147483647
            nbands = ds.RasterCount
            nx = ds.RasterXSize
            ny = ds.RasterYSize
            if not os.path.isfile(dstfp):
                gtiff_options = ['TILED=YES','COMPRESS=LZW','BIGTIFF=IF_SAFER']
                driver = gdal.GetDriverByName('GTiff')
                out_ds = driver.Create(dstfp,nx,ny,nbands,out_datatype,gtiff_options)
                if out_ds:
                    out_ds.SetGeoTransform(ds.GetGeoTransform())
                    out_ds.SetProjection(ds.GetProjection())
                    iinfo = mosaic.ImageInfo(srcfp,"IMAGE")
                    iinfo.get_raster_median()
                    keys = iinfo.median.keys()
                    keys.sort()
                    for band in keys:
                        band_median = iinfo.median[band]
                        if band_median is not None:
                            band_data = ds.GetRasterBand(band)
                            band_nodata = band_data.GetNoDataValue()
                            # default nodata to zero
                            if band_nodata is None:
                                logger.info("Defaulting band {} nodata to zero".format(band))
                                band_nodata = 0.0 
                            band_array = numpy.array(band_data.ReadAsArray())
                            nodata_mask = (band_array==band_nodata)
                   
                            if out_datatype == 3:
                                band_corrected = numpy.full_like(band_array,fill_value=out_nodataval,dtype=numpy.int16)
                            else:
                                band_corrected = numpy.full_like(band_array,fill_value=out_nodataval,dtype=numpy.int32)  
                            band_valid = band_array[~nodata_mask]
                            if band_valid.size != 0:          
                                band_min = numpy.min(band_valid)
                                corr_min = numpy.subtract(float(band_min),float(band_median))
                                if( corr_min < float(out_min) ):
                                    logger.error("BandSubtractMedian() returns min out of range for {} band {}".format(srcfp,band))
                                    return 1
                                band_corrected[~nodata_mask] = numpy.subtract(band_array[~nodata_mask],band_median)
                            else:
                                logger.warning("Band {} has no valid data".format(band))
                            out_band = out_ds.GetRasterBand(band)
                            out_band.WriteArray(band_corrected)
                            out_band.SetNoDataValue(out_nodataval)

                        else:
                            logger.error("BandSubtractMedian(): iinfo.median[{}] is None, image {}".format(band,srcfp))
                            return 1
                else:
                    logger.error("BandSubtractMedian(): !driver.Create({})".format(dstfp))
                    return 1
                ds = None
                out_ds = None

                ## redo pyramids
                cmd = 'gdaladdo "%s" 2 4 8 16' %(srcfp)
                utils.exec_cmd(cmd)

            else:
                logger.info("BandSubtractMedian(): {} exists".format(dstfp))

        else:
            logger.error("BandSubtractMedian(): !gdal.Open({})".format(srcfp))
            return 1
        return 0
    else:
        logger.error("BandSubtractMedian(): !os.path.isfile({})".format(srcfp))
        return 1


if __name__ == '__main__':
    main()
