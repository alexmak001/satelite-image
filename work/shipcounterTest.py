import os
import shutil
import sys
import ee
import geemap
import rasterio
import cv2
from PIL import Image
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from datetime import datetime
from datetime import date
from datetime import timedelta
import joblib
import numpy as np
import ast
import joblib
import torch
import torchvision
import time 

# filepaths
clf_fp = "sean_notebooks/inshore_offshore_clf_normal_model.pkl"
faster_fp = "alex_notebooks/models/faster300ep.pt"
retina_fp = "alex_notebooks/models/retina300R2.pt"
credentials_fp = 'alex_notebooks/models/sar-ship-detection-fb527bcf2a6d.json'

# change these values if needed
retina_threshold = 0.85
faster_threshold = 0.7


# batch size 
batch_size = 4
# clear cuda
import gc

gc.collect()

torch.cuda.empty_cache()

# load in classifier
clf = joblib.load(clf_fp)

# move  model to device
device = torch.device('cuda') if torch.cuda.is_available() else torch.device('cpu')
# load models in 
# load in faster r cnn
faster_rcnn = torchvision.models.detection.fasterrcnn_resnet50_fpn()
num_classes = 2  # 1 class (ship) + background
# get number of input features for the classifier
in_features = faster_rcnn.roi_heads.box_predictor.cls_score.in_features
# replace the pre-trained head with a new one
faster_rcnn.roi_heads.box_predictor = torchvision.models.detection.faster_rcnn.FastRCNNPredictor(in_features, num_classes)
faster_rcnn.load_state_dict(torch.load(faster_fp))
faster_rcnn.eval()
faster_rcnn.to(device)

#load retina
retina = torchvision.models.detection.retinanet_resnet50_fpn(num_classes=2)
retina.load_state_dict(torch.load(retina_fp))
retina.eval()
retina.to(device)
print()

# initialize Earth Engine
service_account = 'snng-download@sar-ship-detection.iam.gserviceaccount.com'
credentials = ee.ServiceAccountCredentials(service_account, credentials_fp)
ee.Initialize(credentials)



def ship_counter(place_coords, start_date, end_date, del_images):

    path = "alex_notebooks/BigData/"

    # downloads all images to a file called gee_data
    print("Downloading Images...")
    #dates = image_downloader(place_coords, start_date, end_date,path)
    print("Finished Downloading Images")
    dates = os.listdir(path)
    fnames = os.listdir(path)

    totalShip_count = []

    print("Counting Ships")
    for file in fnames:
        file_fp = path+file
        # TODO: FIX Function to make it to nearest multiple of 800 OR PAD WITH BLACK
        # splits each image into an m xn array of 800x800
        split_img, img_name = image_splitter(file_fp)
        #print(split_img.shape)
        # TODO: Convert it to a 1d array of images
        # flatten split array into a [m*n, 800, 800] array
        flattened = np.reshape(split_img, (-1, split_img.shape[2], split_img.shape[3]))


        m = flattened.shape[0]
        
        # counts number of ships in each sub image
        subImageCount = 0
        
        # loop through all images and classify them first
        inshoreImg = []
        offshoreImg = []
        
        for i in range(m):
            # get cur image
            curImg = flattened[i]

            # classify to be inshore(0) or offshore (1)
            offshore = inshore_offshore_classifier(curImg) == 1

            #print(img_name,curImg.shape, offshore)
            
            # appends to array
            if offshore:
                offshoreImg.append(curImg)
            else:
                inshoreImg.append(curImg)

        inshoreImg = np.array(inshoreImg)
        offshoreImg = np.array(offshoreImg)

        # print(inshoreImg.shape)
        # print(offshoreImg.shape)

        # converts numpy array of inshore images to tensors for pytorch
        inshoreImg = torch.tensor(inshoreImg,dtype=torch.float32)
        inshoreImg = torch.unsqueeze(inshoreImg, dim=0)
        inshoreImg = inshoreImg.permute(1,0,2,3)
        #inshoreImg = inshoreImg.to(device)

        # counts inshore
        count_in = detect_ships_inshore(inshoreImg)
        #print(count_in)
        #subImageCount += count_in

        # converts numpy array of offshore images to tensors for pytorch
        offshoreImg = torch.tensor(offshoreImg,dtype=torch.float32)
        offshoreImg = torch.unsqueeze(offshoreImg, dim=0)
        offshoreImg = offshoreImg.permute(1,0,2,3)
        #offshoreImg = offshoreImg.to(device)

        #counts offshore 
        # TODO: Implement logic of function
        count_off = detect_ships_offshore(offshoreImg)

        #print(count_in,count_off)

        subImageCount = count_in + count_off
        
        print(file,subImageCount.item())

        totalShip_count.append(subImageCount.item())

    
    # delete images locally
    if del_images:
        shutil.rmtree(path)
        print("file deleted")
    
    
    dates = 1
    return dates, totalShip_count



def image_downloader(place_coords, start_date, end_date, path):
    
    # input given as 'MM/DD/YYYY'
    start_date = datetime.strptime(start_date, '%m/%d/%Y')
    end_date = datetime.strptime(end_date, '%m/%d/%Y')

    
    # create folders if not already created
    if not os.path.exists(path):
        os.mkdir(path)
    
        
    bbox = place_coords
    
    region = ee.Geometry.Polygon(bbox)

    collection = ee.ImageCollection("COPERNICUS/S1_GRD").filterDate(start_date,end_date).filterBounds(region)
    collection_list = collection.toList(collection.size())
    dates = geemap.image_dates(collection, date_format='YYYY-MM-dd').getInfo()
        
    for i, date in enumerate(dates[:]):
        
        image = ee.Image(collection_list.get(i)).select('VV')

        try:
            geemap.ee_export_image(image, filename = path+"{}.tif".format(date), region = region)
        except Exception as e:
            print(e)

    print('Successfully Downloaded')
    
    return dates



def image_splitter(img_fp):
    """
    Takes the image filepath returns m x n array of the subimages
    pads with 0 
    """
    #     img = gdal.Open(img_fp)
    #     img_array = img.GetRasterBand(1).ReadAsArray()
    # note have to add in rescaled helper fn

    # define splitting helper function
    # got this from https://towardsdatascience.com/efficiently-splitting-an-image-into-tiles-in-python-using-numpy-d1bf0dd7b6f7
    def reshape_split(image, kernel_size):
        img_height, img_width = image.shape
        tile_height, tile_width = kernel_size
        
        tiled_array = image.reshape(img_height // tile_height,
                                tile_height,
                                img_width // tile_width,
                                tile_width)
        tiled_array = tiled_array.swapaxes(1,2)
        return tiled_array


    # get image name to return
    # splits full fp by \, then gets the YYYY-MM-DD_#.jpg, then cuts off .jpg 
    img_name = img_fp

    img_array = cv2.imread(img_fp,0)

    # with rasterio.open(img_fp) as src:
    #     img_array = src.read()[0]
    #     print(img_array.shape)
        
    # have to clip values to take away gray tint from image
    # and have to do this so that inshore offshore classifier works
    #img_array = np.clip(img_array, -20, 0)
    
    # img_height, img_width = img_array.shape
    # # get next biggest multiple of 800
    # # TODO: Make it closest multiple of 800 instead OR PAD WITH BLACK
    # new_height = img_height + (800 - img_height % 800)
    # new_width = img_width + (800 - img_width % 800)
    
    # # calculate number of pixels to pad
    # delta_w = new_width - img_width
    # delta_h = new_height - img_height
    # top, bottom = delta_h // 2, delta_h - (delta_h // 2)
    # left, right = delta_w // 2, delta_w - (delta_w // 2)
    
    # # pad image with 0
    # image_pad = np.pad(img_array, ((top, bottom), (left, right)), mode='constant', constant_values=-20)
    
    # # for some reason have to rescale or else when saving image it will be dark
    # rescaled = 255*(image_pad-image_pad.min())/(image_pad.max()-image_pad.min())

    # normalize between 0 and 1
    # MAKE A BETTER RESCALING

    rescale_normalized = img_array / 255
    
    # split image into subimages
        # will return array [m, n, 800, 800] where there are an m x n number of images with size 800x800 
    split = reshape_split(rescale_normalized, kernel_size=(800,800))
    
    return split, img_name




def inshore_offshore_classifier(img):
    """
    Takes in an image and classifies it as either offshore(1) or inshore(0)
    """

    
    img_vals = np.copy(img)
    img_50 = np.percentile(img_vals,50)
    img_80 = np.percentile(img_vals,80)
    img_90 = np.percentile(img_vals,90)
    img_30 = np.percentile(img_vals,30)
    
    features = np.array([[img_50, img_80, img_90, img_30]])
    return clf.predict(features)[0]



def detect_ships_inshore(image):
    
    
    data_batches = torch.split(image, batch_size)

    # Create an empty list to store the predictions
    prediction = []

    # Iterate over the data batches and obtain the predictions for each batch
    with torch.no_grad():
        for batch in data_batches:
            # Move the batch to the device where the model is located
            batch = batch.to(device)
            
            # Make predictions for the batch
            batch_predictions = faster_rcnn(batch)
            # print(batch_predictions)
            # print(len(batch_predictions))

            for pred in batch_predictions:
                # Move the predictions to the CPU and append them to the list
                prediction.append(pred["scores"].cpu())
            # Move the predictions to the CPU and append them to the list
            #prediction.append(batch_predictions.cpu())

    
    totalShip = 0
    # double check predictions
    for i in range(len(prediction)):
        #print("inshore")
        #print(prediction[i]["scores"])
        totalShip += sum(prediction[i]>faster_threshold)
    #print(totalShip)
    return totalShip

# TODO: Implement logic of function
def detect_ships_offshore(image):

    
    data_batches = torch.split(image, batch_size)

    # Create an empty list to store the predictions
    prediction = []

    # Iterate over the data batches and obtain the predictions for each batch
    with torch.no_grad():
        for batch in data_batches:
            # Move the batch to the device where the model is located
            batch = batch.to(device)
            
            # Make predictions for the batch
            batch_predictions = faster_rcnn(batch)
          

            for pred in batch_predictions:
                # Move the predictions to the CPU and append them to the list
                prediction.append(pred["scores"].cpu())
            # Move the predictions to the CPU and append them to the list
         

    
    totalShip = 0
    # double check predictions
    for i in range(len(prediction)):

        totalShip += sum(prediction[i]>faster_threshold)
    
    return totalShip

def main():

    place_coords = [(-118.32027994564326,33.64246038322455),(-118.07789408138545,33.64246038322455),(-118.07789408138545,33.78867573774964),(-118.32027994564326,33.78867573774964),(-118.32027994564326,33.64246038322455)]
    # datetime(start_year, start_month, start_day)
    # string should be formatted as "YEAR MONTH DAY"
    start_date = "01/01/2020"
    end_date = "01/08/2020"
    del_images = False

    t1 = time.time()
    dates, totalShip_count = ship_counter(place_coords, start_date, end_date, del_images)
    t2 = time.time()
    t3 = t2-t1

    print(t3)
    print(dates)
    print(totalShip_count)

    return 

if __name__ == '__main__':
    
    main()
