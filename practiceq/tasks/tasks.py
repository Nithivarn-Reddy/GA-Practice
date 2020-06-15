import random
from celery import chain
from celery.task import task
from subprocess import check_call, check_output
import glob as glob
from celery import Celery
import celeryconfig
from uuid import uuid5, NAMESPACE_DNS
import shutil
from shutil import rmtree
import datetime
from .derivative_utils import _params_as_string,_formatextension,processimage
from .recipe_utils import _get_path
from .utils import *
from .recipe_utils import *



repoUUID = uuid5(NAMESPACE_DNS, 'repository.ou.edu')

# Assert correct generation
assert str(repoUUID) == "eb0ecf41-a457-5220-893a-08b7604b7110"


app = Celery()
app.config_from_object(celeryconfig)

mount_point="/mnt"
ou_derivative_bag_url = "https://bag.ou.edu/derivative"
recipe_url = ou_derivative_bag_url + "/{0}/{1}/{2}.json"
base_url = "https://cc.lib.ou.edu"
api_url = "{0}/api".format(base_url)
search_url = "{0}?query={{\"filter\": {{\"bag\": \"{1}\"}}}}"


bagList=[]

def getAllBags():
    response = requests.get('https://cc.lib.ou.edu/api/catalog/data/catalog/digital_objects/?query={"filter":{"department":"DigiLab","project":{"$ne":"private"},"locations.s3.exists":{"$eq":true},"derivatives.jpeg_040_antialias.recipe":{"$exists":false,"$error":ne}}}&format=json&page_size=0')
    jobj = response.json()
    results=jobj.get('results')
    for obj in results:
        yield obj.get('bag')

@task
def getSample(size=4):
    try:
        #list(random.sample(list(getAllBags()), size))
        yield ['Apian_1545','Abbati_1703']
    except:
        return getAllBags()
@task
def automate(outformat,filter,scale,crop,force_overwrite=False,bag=None):
    """
    This automates the process of derivative creation.
    :return: string "kicked off or not"
    """
    # If bag is given is then kickoff separate chain.

    for bag in getSample():
        result = chain(read_source_update_derivative.s(bag, "source", "derivative", outformat, filter, scale,force_overwrite),
                       process_recipe.s())
    """
    if bag:
        result = chain(read_source_update_derivative.s(bag,"source","derivative",outformat,filter,scale=0.4),process_recipe.s())
    else:
        result = chain(getSample.s(),read_source_update_derivative.s("source","derivative",outformat,filter,scale=0.4),process_recipe.s())
    """
    result.delay()
    return "automate kicked off"

def listpagefiles(bag_name, paramstring):
    filename = "{0}.json".format(bag_name)
    path=_get_path(bag_name, paramstring)
    recipe_json = os.path.join(path,filename)
    with open(recipe_json,"r") as f:
        recipe =  loads(f.read())
    return [page['file'] for page in recipe['recipe']['pages']]

def update_catalog(bag,paramstring,mmsid=None):
    db_client = app.backend.database.client
    collection = db_client.cybercom.catalog
    query = {"bag": bag}
    document = collection.find_one(query)
    if document == None:
        return False
    document_id = document['_id']
    myquery = {"_id":document_id}

    if mmsid == None:
        if "error" in document["application"]["islandora"].keys():
            document["application"]["islandora"]["error"].append("mmsid not found")
        else:
            document["application"]["islandora"].update({"error": ["mmsid not found"]})
        update_mmsid_error = {
            "$set":
                {
                    "application":
                        {
                            "islandora": document["application"]["islandora"]
                        }
                }
        }
        status = collection.update_one(myquery,update_mmsid_error)
        return status.raw_result['nModified'] != 0
        #return status
    if paramstring not in document["derivatives"]:
        document["derivatives"][paramstring]={}
    #ask whether bag_name needs to be lower.
    document["derivatives"][paramstring]["recipe"] = recipe_url.format(bag, paramstring, bag)
    document["derivatives"][paramstring]["datetime"] = datetime.datetime.utcnow().isoformat()
    document["derivatives"][paramstring]["pages"] = listpagefiles(bag, paramstring)
    update_derivative_values = {
        "$set":
            {
                "derivatives":
                    {
                        paramstring: document["derivatives"][paramstring]
                    }
            }
    }
    general_update_status = collection.update_one(myquery,update_derivative_values)
    return general_update_status.raw_result['nModified'] !=0
    #return general_update_status

class derivative_generation_error(Exception):
    pass

@task
def read_source_update_derivative(bags,s3_source="source",s3_destination="derivative",outformat="JPEG",filter='ANTIALIAS',scale=None, crop=None,force_overwrite=False):
    """
    bagname = List containing bagnames eg : [bag1,bag2...]
    source = source file.
    outformat = "TIFF or jpeg or jpg or tif"
    filter = "ANTALIAS" - filter type of the image.
    scale = At what scale do you need the reduction of the size - eg 0.40 or o.20
    crop = size in which the image needs to be cropped, Provide it as a list - eg - [10,10,20,40]

    """
    bags_with_mmsids = OrderedDict()
    if type(bags) == 'str':
        bags = [bags]
    for bag in bags:
        task_id = str(read_source_update_derivative.request.id)
        formatparams = _params_as_string(outformat,filter,scale,crop)

        path_to_bag = "{0}/{1}/{2}/".format(mount_point,s3_source,bag)
        mmsid =get_mmsid(bag,path_to_bag)
        if mmsid:
            bags_with_mmsids[bag]=OrderedDict()
            bags_with_mmsids[bag]['mmsid']=mmsid
            path_to_tif_files_of_bag = "{0}/{1}/{2}/data/*.tif".format(mount_point,s3_source,bag)
            print(path_to_tif_files_of_bag)
            outdir = "{0}/{1}/{2}/{3}".format(mount_point,s3_destination,bag,formatparams)

            #ask regarding this removal.
            if os.path.exists("{0}/derivative/{1}/{2}".format(mount_point, bag, formatparams)) and force_overwrite:
                rmtree(outdir)
            if os.path.exists("{0}/derivative/{1}/{2}".format(mount_point, bag, formatparams)) and not force_overwrite:
                raise derivative_generation_error("derivative already exists and force_overwrite is set to false")
            if not os.path.exists("{0}/derivative/{1}/{2}".format(mount_point, bag, formatparams)):
                os.makedirs(outdir)
            for file in glob.glob(path_to_tif_files_of_bag):
                print(file)
                outpath = '{0}/{1}/{2}/{3}/{4}.{5}'.format(mount_point,"derivative",bag,formatparams,file.split('/')[-1].split('.')[0].lower(),_formatextension(outformat))
                processimage(inpath=file,outpath=outpath,outformat=_formatextension(outformat),filter=filter,scale=scale,crop=crop)
        else:
            update_catalog(bag,formatparams,mmsid)
    return {"local_derivatives": "{0}/oulib_tasks/{1}".format(base_url, task_id), "s3_destination": s3_destination,
            "task_id": task_id,"bags":bags_with_mmsids,"format_params":formatparams}

@task
def process_recipe(derivative_args):
    """
        This function generates the recipe file and returns the json structure for each bag.

        params:
        derivative_args:The arguments returned by readSource_updateDerivative function.
    """
    #task_id= derivative_args.get('task_id')
    bags = derivative_args.get('bags') #bags = { "bagname1" : { "mmsid": value} , "bagName2":{"mmsid":value}, ..}
    formatparams = derivative_args.get('format_params')
    #formatparams="jpeg_040_antialias"
    #bags={"Abbati_1703":{"mmsid":9932140502042}}
    for bag_name,mmsid in bags.items():
        bag_derivative(bag_name,formatparams)
        recipe_file_creation(bag_name,mmsid,formatparams)
        update_catalog(bag_name,formatparams,mmsid["mmsid"])
        return "derivative-recipe file of bag is generated"

@task
def insert_data_into_mongoDB():
    response = requests.get('https://cc.lib.ou.edu/api/catalog/data/catalog/digital_objects/?query={"filter":{"department":"DigiLab","project":{"$ne":"private"},"locations.s3.exists":{"$eq":true},"derivatives.jpeg_040_antialias.recipe":{"$exists":false}}}&format=json&page_size=0')
    jobj = response.json()
    results = jobj.get('results')
    db_client = app.backend.database.client
    print(db_client.database_names())
    database = db_client["cybercom"]

    if not "catalog" in db_client.cybercom.collection_names():
        mycol = database["catalog"]
        for data in results:
            mycol.insert_one(data)
        return "successful"
    return "already exists"
   # mydict = {"name": "hello", "address": "Norman"}
    #x = mycol.insert_one(mydict)


