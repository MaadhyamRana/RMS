""" Perform star extraction and meteor detection on a given folder, archive detections, upload to server. """


from __future__ import print_function, division, absolute_import


import os
import sys
import traceback
import argparse
import logging
import random
import glob

from RMS.ArchiveDetections import archiveDetections, archiveFieldsums
# from RMS.Astrometry.ApplyAstrometry import applyAstrometryFTPdetectinfo
from RMS.Astrometry.ApplyRecalibrate import recalibrateIndividualFFsAndApplyAstrometry
from RMS.Astrometry.CheckFit import autoCheckFit
import RMS.ConfigReader as cr
from RMS.DownloadPlatepar import downloadNewPlatepar
from RMS.DetectStarsAndMeteors import detectStarsAndMeteorsDirectory, saveDetections
from RMS.Formats.CAL import writeCAL
from RMS.Formats.FFfile import validFFName
from RMS.Formats.FTPdetectinfo import readFTPdetectinfo, writeFTPdetectinfo
from RMS.Formats.Platepar import Platepar
from RMS.Formats import CALSTARS
from RMS.MLFilter import filterFTPdetectinfoML
from RMS.UploadManager import UploadManager
from RMS.Routines.Image import saveImage
from RMS.Routines.MaskImage import loadMask
from Utils.CalibrationReport import generateCalibrationReport
from Utils.Flux import prepareFluxFiles
from Utils.FOVKML import fovKML
from Utils.GenerateTimelapse import generateTimelapse, generateTimelapseFromFrames
from Utils.MakeFlat import makeFlat
from Utils.PlotFieldsums import plotFieldsums
from Utils.RMS2UFO import FTPdetectinfo2UFOOrbitInput
from Utils.ShowerAssociation import showerAssociation
from Utils.PlotTimeIntervals import plotFFTimeIntervals
from Utils.TimestampRMSVideos import timestampRMSVideos
from RMS.Formats.ObservationSummary import addObsParam, getObsDBConn, nightSummaryData
from RMS.Formats.ObservationSummary import serialize, startObservationSummaryReport, finalizeObservationSummary
from Utils.AuditConfig import compareConfigs
from RMS.Misc import RmsDateTime


# Get the logger from the main module
log = logging.getLogger("logger")



def getPlatepar(config, night_data_dir):
    """ Downloads a new platepar from the server or uses an existing one.
    
    Arguments:  
        Config: [Config instance]  
        night_data_dir: [str] Full path to the data directory.  

    Return:  
        platepar, platepar_path, platepar_fmt  
    """


    # Download a new platepar from the server, if present  
    downloadNewPlatepar(config)


    # Construct path to the platepar in the night directory
    platepar_night_dir_path = os.path.join(night_data_dir, config.platepar_name)

    # Load the default platepar from the RMS if it is available
    platepar = None
    platepar_fmt = None
    platepar_path = os.path.join(config.config_file_path, config.platepar_name)
    if os.path.exists(platepar_path):
        platepar = Platepar()
        platepar_fmt = platepar.read(platepar_path, use_flat=config.use_flat)

        log.info('Loaded platepar from RMS directory: ' + platepar_path)


    # Otherwise, try to find the platepar in the data directory
    elif os.path.exists(platepar_night_dir_path):

        platepar_path = platepar_night_dir_path

        platepar = Platepar()
        platepar_fmt = platepar.read(platepar_path, use_flat=config.use_flat)

        log.info('Loaded platepar from night directory: ' + platepar_path)

    else:

        log.info('No platepar file found!')


    if platepar is not None:
        
        # Make sure that the station code from the config and the platepar match
        if platepar.station_code is not None:
            if config.stationID != platepar.station_code:

                # If they don't match, don't use this platepar
                log.info("The station code in the platepar doesn't match the station code in config file! Not using the platepar...")

                platepar = None
                platepar_fmt = None

            else:

                # Update the geo location in the platepar using values in the config file
                platepar.lat = config.latitude
                platepar.lon = config.longitude
                platepar.elev = config.elevation

        
        # Make sure the config and the platepar FOV are within a factor of two
        if (platepar.fov_h is not None) and (platepar.fov_v is not None):
            
            # Calculate the diagonal FOV for both the platepar and the config
            pp_fov_diag = (platepar.fov_h**2 + platepar.fov_v**2)**0.5
            config_fov_diag = (config.fov_w**2 + config.fov_h**2)**0.5

            # Compute the ratio of the FOVs
            fov_ratio = pp_fov_diag/config_fov_diag

            # If the ratio is smaller than 0.5 or greater than 2, don't use this platepar
            if (fov_ratio < 0.5) or (fov_ratio > 2):
                    
                # If they don't match, don't use this platepar
                log.info("The FOV in the platepar is not within a factor of 2 of the FOV in the config file! Not using the platepar...")

                platepar = None
                platepar_fmt = None


    # Make sure the image resolution matches
    if platepar is not None:
        if (int(config.width) != int(platepar.X_res)) or (int(config.height) != int(platepar.Y_res)):

            # If they don't match, don't use this platepar
            log.info("The image resolution in config and platepar don't match! Not using the platepar...")

            platepar = None
            platepar_fmt = None

        

    return platepar, platepar_path, platepar_fmt




def processNight(night_data_dir, config, detection_results=None, nodetect=False):
    """ Given the directory with FF files, run detection and archiving.  
    
    Arguments:  
        night_data_dir: [str] Path to the directory with FF files.  
        config: [Config obj]  

    Keyword arguments:  
        detection_results: [list] An optional list of detection. If None (default), detection will be done
            on the the files in the folder.  
        nodetect: [bool] True if detection should be skipped. False by default.  

    Return:  
        night_archive_dir: [str] Path to the night directory in ArchivedFiles.  
        archive_name: [str] Path to the archive.  
        detector: [QueuedPool instance] Handle to the detector.  
    """

    # Remove final slash in the night dir
    if night_data_dir.endswith(os.sep):
        night_data_dir = night_data_dir[:-1]

    # Extract the name of the night
    night_data_dir_name = os.path.basename(os.path.abspath(night_data_dir))

    platepar = None
    kml_files = []
    recalibrated_platepars = None
    
    # If the detection should be run
    if (not nodetect):

        # If no detection was performed, run it
        if detection_results is None:

            # Run detection on the given directory
            calstars_name, ftpdetectinfo_name, ff_detected, \
                detector = detectStarsAndMeteorsDirectory(night_data_dir, config)

        # Otherwise, save detection results
        else:

            # Save CALSTARS and FTPdetectinfo to disk
            calstars_name, ftpdetectinfo_name, ff_detected = saveDetections(detection_results, \
                night_data_dir, config)

            # If the files were previously detected, there is no detector
            detector = None




        obs_db_conn = getObsDBConn(config)
        # Filter out detections using machine learning
        if config.ml_filter > 0:

            log.info("Filtering out detections using machine learning...")

            ff_detected = filterFTPdetectinfoML(config, os.path.join(night_data_dir, ftpdetectinfo_name), \
                threshold=config.ml_filter, keep_pngs=False, clear_prev_run=True)
            addObsParam(obs_db_conn, "detections_after_ml", len(ff_detected))

        addObsParam(obs_db_conn,"detections_after_ml", len(readFTPdetectinfo(night_data_dir,ftpdetectinfo_name)))
        obs_db_conn.close()

        # Get the platepar file
        platepar, platepar_path, platepar_fmt = getPlatepar(config, night_data_dir)


        # Run calibration check and auto astrometry refinement
        if (platepar is not None) and (calstars_name is not None):

            # Read in the CALSTARS file
            calstars_list = CALSTARS.readCALSTARS(night_data_dir, calstars_name)


            # Run astrometry check and refinement
            platepar, fit_status = autoCheckFit(config, platepar, calstars_list)

            obs_db_conn = getObsDBConn(config)
            # If the fit was successful, apply the astrometry to detected meteors
            if fit_status:

                log.info('Astrometric calibration SUCCESSFUL!')
                addObsParam(obs_db_conn, "photometry_good", "True")
                # Save the refined platepar to the night directory and as default
                platepar.write(os.path.join(night_data_dir, config.platepar_name), fmt=platepar_fmt)
                platepar.write(platepar_path, fmt=platepar_fmt)

            else:
                log.info('Astrometric calibration FAILED!, Using old platepar for calibration...')
                addObsParam(obs_db_conn, "photometry_good", "False")

            obs_db_conn.close()
            # If a flat is used, disable vignetting correction
            if config.use_flat:
                platepar.vignetting_coeff = 0.0



            log.info("Recalibrating astrometry on FF files with detections...")

            # Recalibrate astrometry on every FF file and apply the calibration to detections
            recalibrated_platepars = recalibrateIndividualFFsAndApplyAstrometry(night_data_dir, \
                os.path.join(night_data_dir, ftpdetectinfo_name), calstars_list, config, platepar)




            log.info("Converting RMS format to UFOOrbit format...")

            # Convert the FTPdetectinfo into UFOOrbit input file
            FTPdetectinfo2UFOOrbitInput(night_data_dir, ftpdetectinfo_name, platepar_path)



            # Generate a calibration report
            log.info("Generating a calibration report...")
            try:
                generateCalibrationReport(config, night_data_dir, platepar=platepar)

            except Exception as e:
                log.debug('Generating calibration report failed with the message:\n' + repr(e))
                log.debug(repr(traceback.format_exception(*sys.exc_info())))



            # Perform single station shower association
            log.info("Performing single station shower association...")
            try:
                showerAssociation(config, [os.path.join(night_data_dir, ftpdetectinfo_name)], \
                    save_plot=True, plot_activity=True, color_map=config.shower_color_map)

            except Exception as e:
                log.debug('Shower association failed with the message:\n' + repr(e))
                log.debug(repr(traceback.format_exception(*sys.exc_info())))



            # Generate the FOV KML file
            log.info("Generating a FOV KML file...")
            try:

                mask_path = None
                mask = None

                # Get the path to the default mask
                mask_path_default = os.path.join(config.config_file_path, config.mask_file)

                # Try loading the mask from the night directory
                if os.path.exists(os.path.join(night_data_dir, config.mask_file)):
                    mask_path = os.path.join(night_data_dir, config.mask_file)

                # Try loading the default mask if the mask is not in the night directory
                elif os.path.exists(mask_path_default):
                    mask_path = os.path.abspath(mask_path_default)

                # Load the mask if given
                if mask_path:
                    mask = loadMask(mask_path)

                if mask is not None:
                    log.info("Loaded mask: {:s}".format(mask_path))

                # Generate the KML (only the FOV is shown, without the station) - 100 km
                kml_file100 = fovKML(night_data_dir, platepar, mask=mask, plot_station=False, \
                    area_ht=100000)
                kml_files.append(kml_file100)


                # Generate the KML (only the FOV is shown, without the station) - 70 km
                kml_file70 = fovKML(night_data_dir, platepar, mask=mask, plot_station=False, \
                    area_ht=70000)
                kml_files.append(kml_file70)

                # Generate the KML (only the FOV is shown, without the station) - 25 km
                kml_file25 = fovKML(night_data_dir, platepar, mask=mask, plot_station=False, \
                    area_ht=25000)
                kml_files.append(kml_file25)



            except Exception as e:
                log.debug("Generating a FOV KML file failed with the message:\n" + repr(e))
                log.debug(repr(traceback.format_exception(*sys.exc_info())))



            # Prepare the flux files
            log.info("Preparing flux files...")
            try:
                prepareFluxFiles(config, night_data_dir, os.path.join(night_data_dir, ftpdetectinfo_name),
                                 mask=mask, platepar=platepar)

            except Exception as e:
                log.debug("Preparing flux files failed with the message:\n" + repr(e))
                log.debug(repr(traceback.format_exception(*sys.exc_info())))


    else:
        ff_detected = []
        detector = None




    log.info('Plotting field sums...')

    # Plot field sums
    try:
        plotFieldsums(night_data_dir, config)

    except Exception as e:
        log.debug('Plotting field sums failed with message:\n' + repr(e))
        log.debug(repr(traceback.format_exception(*sys.exc_info())))



    # Archive all fieldsums to one archive
    archiveFieldsums(night_data_dir)


    # If videos were saved, rename them with the timestamp of the first frame
    # This command requires the FS archive to be present
    # if config.raw_video_save:

    #     try:
    #         timestampRMSVideos(config.video_dir, rename=True)

    #     except Exception as e:
    #         log.debug('Renaming videos failed with the message:\n' + repr(e))
    #         log.debug(repr(traceback.format_exception(*sys.exc_info())))


    # List for any extra files which will be copied to the night archive directory. Full paths have to be 
    #   given
    extra_files = []


    log.info('Making a flat...')

    # Make a new flat field image
    try:
        flat_img = makeFlat(night_data_dir, config)

    except Exception as e:
        log.debug('Making a flat failed with message:\n' + repr(e))
        log.debug(repr(traceback.format_exception(*sys.exc_info())))
        flat_img = None
        

    # If making flat was successful, save it
    if flat_img is not None:

        # Save the flat in the night directory, to keep the operational flat updated
        flat_path = os.path.join(night_data_dir, os.path.basename(config.flat_file))
        saveImage(flat_path, flat_img)
        log.info('Flat saved to: ' + flat_path)

        # Copy the flat to the night's directory as well
        extra_files.append(flat_path)

    else:
        log.info('Making flat image FAILED!')



    # Generate a timelapse
    if config.timelapse_generate_captured:
        
        log.info('Generating a timelapse...')
        try:

            # Make the name of the timelapse file
            timelapse_file_name = night_data_dir_name.replace("_detected", "") + "_timelapse.mp4"

            # Generate the timelapse
            generateTimelapse(night_data_dir, output_file=timelapse_file_name)

            timelapse_path = os.path.join(night_data_dir, timelapse_file_name)

            # Add the timelapse to the extra files
            extra_files.append(timelapse_path)

        except Exception as e:
            log.debug('Generating a timelapse failed with message:\n' + repr(e))
            log.debug(repr(traceback.format_exception(*sys.exc_info())))

    log.info('Plotting timestamp intervals...')

    # Plot timestamp intervals
    try:
        jitter_quality, dropped_frame_rate, intervals_path = plotFFTimeIntervals(night_data_dir, fps=config.fps)

        if jitter_quality is not None and dropped_frame_rate is not None:
            log.info('Timestamp Intervals Analysis: Jitter Quality: {:.1f}%, Dropped Frame Rate: {:.1f}%'
                     .format(jitter_quality, dropped_frame_rate))
            
        else:
            log.info('Timestamp Intervals Analysis: Failed')

        # Add the timelapse to the extra files
        if intervals_path is not None:
            extra_files.append(intervals_path)
        obs_db_conn = getObsDBConn(config)
        addObsParam(obs_db_conn,"jitter_quality",jitter_quality)
        addObsParam(obs_db_conn,"dropped_frame_rate",dropped_frame_rate)
        obs_db_conn.close()

    except Exception as e:
        log.debug('Plotting timestamp interval failed with message:\n' + repr(e))
        log.debug(repr(traceback.format_exception(*sys.exc_info())))

    # Generate a config audit report
    log.info('Generate config audit report')
    try:
        # Make the name of the audit file
        audit_file_name = night_data_dir_name.replace("_detected", "") + "_config_audit_report.txt"

        # Construct the full path for files
        audit_file_path = os.path.join(night_data_dir, audit_file_name)
        config_file_path = os.path.join(night_data_dir, ".config")

        with open(audit_file_path, 'w') as f:
            f.write(compareConfigs(config_file_path,
                                   os.path.join(config.rms_root_dir, ".configTemplate"),
                                   os.path.join(config.rms_root_dir, "RMS/ConfigReader.py")))

        extra_files.append(audit_file_path)

    except Exception as e:
        log.debug('Generating config audit failed with message:\n' + repr(e))
        log.debug(repr(traceback.format_exception(*sys.exc_info())))


    # Generate a timelapse from frames
    if config.timelapse_generate_from_frames:

        log.info('Generating timelapse from saved frames...')
        try:
            frame_dir = os.path.join(config.data_dir, config.frame_dir)

            # Generate timelapse for each day of the year, if not present
            for year in os.listdir(frame_dir):
                # Each 'year' is 2024, 2025, ...
                year_dir = os.path.join(frame_dir, year)


                for day in os.listdir(year_dir):
                    # Each 'day' is 20240923-267, 20240924-268, ...
                    day_dir = os.path.join(year_dir, day)

                    # Skip if not directory or if inside today's directory
                    if (not os.path.isdir(day_dir)) or (day == RmsDateTime.utcnow().strftime("%Y%m%d-%j")):
                        continue
                    
                    img_count = 0


                    # Checking frames for each hour
                    for hour in os.listdir(day_dir):
                        # Each 'hour' is 20240923-267_00, 20240923-267_01, ...
                        hour_dir = os.path.join(day_dir, hour)

                        # Skip if not directory
                        if not os.path.isdir(hour_dir):
                            continue

                        # Count both .jpg and .png files in the hourly subdirectory(s)
                        img_count += len(glob.glob(os.path.join(hour_dir, '*.jpg')) + \
                                         glob.glob(os.path.join(hour_dir, '*.png')))


                    if img_count < 2:
                        # Skip this directory if fewer than 2 JPG files are found
                        continue

                    # Search for current day's timelapse in the corresponding year's directory
                    found_files = glob.glob(os.path.join(year_dir, "{}_frames_timelapse.mp4".format(day)))

                    # If not found, generate timelapse for the current day
                    if not found_files:
                        log.info("No frames timelapse for {} found in {}, generating new timelapse...".format(day, year_dir))

                        # Make the name of the timelapse file from day directory
                        # The day's timelapse and its frametimes.json are both stored in their corresponding year's directory
                        frames_timelapse_path = os.path.join(year_dir, "{}_{}_frames_timelapse.mp4".format(config.stationID, day))
                        timelapse_json_path = os.path.join(year_dir, "{}_{}_frametimes.json".format(config.stationID, day))

                        # Generate the timelapse and cleanup
                        generateTimelapseFromFrames(day_dir, frames_timelapse_path, cleanup_mode='tar')

                        # Add the timelapse and its frametimes.json to the extra files
                        extra_files.append(frames_timelapse_path)
                        extra_files.append(timelapse_json_path)


        except Exception as e:
            log.debug('Generating JPEG timelapse failed with message:\n' + repr(e))
            log.debug(repr(traceback.format_exception(*sys.exc_info())))



    ### Add extra files to archive

    # Add the config file to the archive too
    extra_files.append(config.config_file_name)

    # Add the mask
    if (not nodetect):
        mask_path_default = os.path.join(config.config_file_path, config.mask_file)
        if os.path.exists(mask_path_default):
            mask_path = os.path.abspath(mask_path_default)
            extra_files.append(mask_path)


    # Add the platepar to the archive if it exists
    if (not nodetect):
        if os.path.exists(platepar_path):
            extra_files.append(platepar_path)


    # Add the json file with recalibrated platepars to the archive
    if (not nodetect):
        recalibrated_platepars_path = os.path.join(night_data_dir, config.platepars_recalibrated_name)
        if os.path.exists(recalibrated_platepars_path):
            extra_files.append(recalibrated_platepars_path)

    # Add the FOV KML files
    if len(kml_files):
        extra_files += kml_files


    # Add all flux related files
    if (not nodetect):
        for file_name in sorted(os.listdir(night_data_dir)):
            if ("flux" in file_name) and (file_name.endswith(".json") or file_name.endswith(".ecsv")):
                extra_files.append(os.path.join(night_data_dir, file_name))


    # If FFs are not uploaded, choose two to upload
    if config.upload_mode > 1:
    
        # If all FF files are not uploaded, add two FF files which were successfully recalibrated
        recalibrated_ffs = []
        if recalibrated_platepars is not None:
            for ff_name in recalibrated_platepars:

                pp = recalibrated_platepars[ff_name]

                # Check if the FF was recalibrated
                if pp.auto_recalibrated:
                    recalibrated_ffs.append(os.path.join(night_data_dir, ff_name))

        # Choose two files randomly
        if len(recalibrated_ffs) > 2:
            extra_files += random.sample(recalibrated_ffs, 2)

        elif len(recalibrated_ffs) > 0:
            extra_files += recalibrated_ffs


        # If no were recalibrated
        else:

            # Create a list of all FF files
            ff_list = [os.path.join(night_data_dir, ff_name) for ff_name in os.listdir(night_data_dir) \
                if validFFName(ff_name)]

            # Add any two FF files
            if len(ff_list) > 2:
                extra_files += random.sample(ff_list, 2)
            else:
                extra_files += ff_list
        

    ### ###



    # If the detection should be run
    if (not nodetect):

        # Make a CAL file and a special CAMS FTPdetectinfo if full CAMS compatibility is desired
        if (config.cams_code > 0) and (platepar is not None):

            log.info('Generating a CAMS FTPdetectinfo file...')

            # Write the CAL file to disk
            cal_file_name = writeCAL(night_data_dir, config, platepar)

            # Check if the CAL file was successfully generated
            if cal_file_name is not None:

                cams_code_formatted = "{:06d}".format(int(config.cams_code))

                # Load the FTPdetectinfo
                _, fps, meteor_list = readFTPdetectinfo(night_data_dir, ftpdetectinfo_name, \
                    ret_input_format=True)

                # Replace the camera code with the CAMS code
                for met in meteor_list:

                    # Replace the station name and the FF file format
                    ff_name = met[0]
                    ff_name = ff_name.replace('.fits', '.bin')
                    ff_name = ff_name.replace(config.stationID, cams_code_formatted)
                    met[0] = ff_name


                # Write the CAMS compatible FTPdetectinfo file
                writeFTPdetectinfo(meteor_list, night_data_dir, \
                    ftpdetectinfo_name.replace(config.stationID, cams_code_formatted),\
                    night_data_dir, cams_code_formatted, fps, calibration=cal_file_name, \
                    celestial_coords_given=(platepar is not None))

    try:
        observation_summary_path_file_name, observation_summary_json_path_file_name = (
                finalizeObservationSummary(config, night_data_dir))
        log.info("\n\nObservation Summary\n===================\n\n" + serialize(config) + "\n\n")

    except Exception as e:
        log.debug('Generating Observation Summary failed with message:\n' + repr(e))
        log.debug(repr(traceback.format_exception(*sys.exc_info())))


    extra_files.append(observation_summary_path_file_name)
    extra_files.append(observation_summary_json_path_file_name)
    night_archive_dir = os.path.join(os.path.abspath(config.data_dir), config.archived_dir,
        night_data_dir_name)



    log.info('Archiving detections to ' + night_archive_dir)
    
    # Archive the detections
    archive_name = archiveDetections(night_data_dir, night_archive_dir, ff_detected, config, \
        extra_files=extra_files)


    return night_archive_dir, archive_name, detector




if __name__ == "__main__":

    ### COMMAND LINE ARGUMENTS

    # Init the command line arguments parser
    arg_parser = argparse.ArgumentParser(description="Reprocess the given folder, perform detection, archiving and server upload.")

    arg_parser.add_argument('dir_path', nargs=1, metavar='DIR_PATH', type=str, \
        help='Path to the folder with FF files.')

    arg_parser.add_argument('-c', '--config', nargs=1, metavar='CONFIG_PATH', type=str, \
        help="Path to a config file which will be used instead of the default one.")
    
    arg_parser.add_argument('--num_cores', metavar='NUM_CORES', type=int, default=None, \
        help="Number of cores to use for detection. Default is what is specific in the config file. " 
        "If not given in the config file, all available cores will be used."
        )

    # Parse the command line arguments
    cml_args = arg_parser.parse_args()

    #########################

    # Load the config file
    config = cr.loadConfigFromDirectory(cml_args.config, cml_args.dir_path)

    
    ### Init the logger

    from RMS.Logger import initLogging
    initLogging(config, 'reprocess_')

    log = logging.getLogger("logger")

    ######

    
    # Set the number of cores to use if given
    if cml_args.num_cores is not None:
        config.num_cores = cml_args.num_cores

        if config.num_cores <= 0:
            config.num_cores = -1

            log.info("Using all available cores for detection.")


    duration, _,_,_,_,_,_, = nightSummaryData(config, cml_args.dir_path[0])
    log.info(startObservationSummaryReport(config, duration, force_delete=False))
    # Process the night
    _, archive_name, detector = processNight(cml_args.dir_path[0], config)


    # Upload the archive, if upload is enabled
    if config.upload_enabled:

        # Init the upload manager
        log.info('Starting the upload manager...')

        upload_manager = UploadManager(config)
        upload_manager.start()

        # Add file for upload
        log.info('Adding file to upload list: ' + archive_name)
        upload_manager.addFiles([archive_name])

        # Stop the upload manager
        if upload_manager.is_alive():
            upload_manager.stop()
            log.info('Closing upload manager...')


        # Delete detection backup files
        if detector is not None:
            detector.deleteBackupFiles()
