#!/usr/bin/env python3
"""Generate a relocatable, dependency-free Xcode project for shared SwiftUI sources."""
import hashlib,json,plistlib
from pathlib import Path

ROOT=Path(__file__).resolve().parent.parent
NATIVE=ROOT/"native"
def ident(value):return hashlib.sha1(value.encode()).hexdigest()[:24].upper()
objects={}
def add(key,**value):i=ident(key);objects[i]=value;return i
def render(value):
    if isinstance(value,dict):return "{\n"+"\n".join(json.dumps(str(k))+" = "+render(v)+";" for k,v in value.items())+"\n}"
    if isinstance(value,list):return "("+", ".join(render(v) for v in value)+")"
    return json.dumps(str(value))

def main():
    sources=[]
    for path in sorted((NATIVE/"Sources").glob("*.swift")):
        ref=add(str(path.relative_to(NATIVE)),isa="PBXFileReference",lastKnownFileType="sourcecode.swift",path=str(path.relative_to(NATIVE)),sourceTree="<group>")
        sources.append(ref)
    assets=add("assets",isa="PBXFileReference",lastKnownFileType="folder.assetcatalog",path="Assets.xcassets",sourceTree="<group>")
    products=[];targets=[]
    for name,platform in (("JurvisMac","macosx"),("JurvisMobile","iphoneos")):
        product=add(name+"product",isa="PBXFileReference",explicitFileType="wrapper.application",includeInIndex=0,path="Jurvis.app",sourceTree="BUILT_PRODUCTS_DIR");products.append(product)
        builds=[add(name+s,isa="PBXBuildFile",fileRef=s) for s in sources]
        phase=add(name+"sources",isa="PBXSourcesBuildPhase",buildActionMask=2147483647,files=builds,runOnlyForDeploymentPostprocessing=0)
        resources=add(name+"resources",isa="PBXResourcesBuildPhase",buildActionMask=2147483647,files=[add(name+"assets",isa="PBXBuildFile",fileRef=assets)],runOnlyForDeploymentPostprocessing=0)
        frameworks=add(name+"frameworks",isa="PBXFrameworksBuildPhase",buildActionMask=2147483647,files=[],runOnlyForDeploymentPostprocessing=0)
        configs=[]
        for config in ("Debug","Release"):
            settings={"PRODUCT_NAME":"Jurvis","PRODUCT_BUNDLE_IDENTIFIER":"app.jurvis."+("mac" if platform=="macosx" else "mobile"),"SWIFT_VERSION":"5.0","SDKROOT":platform,
                      "SUPPORTED_PLATFORMS":"macosx" if platform=="macosx" else "iphoneos iphonesimulator","INFOPLIST_FILE":"Info.plist","GENERATE_INFOPLIST_FILE":"YES",
                      "CURRENT_PROJECT_VERSION":"1","MARKETING_VERSION":"0.1.0","JURVIS_WORKSPACE_ROOT":"$(SRCROOT)/..","ASSETCATALOG_COMPILER_APPICON_NAME":"AppIcon",
                      "SWIFT_OPTIMIZATION_LEVEL":"-Onone" if config=="Debug" else "-O","ENABLE_APP_SANDBOX":"NO","CODE_SIGN_STYLE":"Automatic"}
            if platform=="macosx":settings.update(MACOSX_DEPLOYMENT_TARGET="15.0",CODE_SIGN_IDENTITY="-",COMBINE_HIDPI_IMAGES="YES")
            else:settings.update(IPHONEOS_DEPLOYMENT_TARGET="18.0",TARGETED_DEVICE_FAMILY="1,2",SUPPORTS_MACCATALYST="NO",INFOPLIST_KEY_UIApplicationSceneManifest_Generation="YES")
            configs.append(add(name+config,isa="XCBuildConfiguration",buildSettings=settings,name=config))
        configs_id=add(name+"configs",isa="XCConfigurationList",buildConfigurations=configs,defaultConfigurationIsVisible=0,defaultConfigurationName="Debug")
        targets.append(add(name,isa="PBXNativeTarget",buildConfigurationList=configs_id,buildPhases=[phase,frameworks,resources],buildRules=[],dependencies=[],name=name,productName="Jurvis",productReference=product,productType="com.apple.product-type.application"))
    productgroup=add("products",isa="PBXGroup",children=products,name="Products",sourceTree="<group>")
    group=add("main",isa="PBXGroup",children=[*sources,assets,productgroup],sourceTree="<group>")
    configs=[add("project"+c,isa="XCBuildConfiguration",buildSettings={"CLANG_ENABLE_MODULES":"YES","SWIFT_STRICT_CONCURRENCY":"minimal"},name=c) for c in ("Debug","Release")]
    configs_id=add("projectconfigs",isa="XCConfigurationList",buildConfigurations=configs,defaultConfigurationIsVisible=0,defaultConfigurationName="Debug")
    project=add("project",isa="PBXProject",attributes={"LastUpgradeCheck":"2700","BuildIndependentTargetsInParallel":"YES"},buildConfigurationList=configs_id,compatibilityVersion="Xcode 14.0",developmentRegion="zh-Hans",hasScannedForEncodings=0,knownRegions=["zh-Hans","en","Base"],mainGroup=group,productRefGroup=productgroup,projectDirPath="",projectRoot="",targets=targets)
    folder=NATIVE/"Jurvis.xcodeproj";folder.mkdir(exist_ok=True)
    (folder/"project.pbxproj").write_text("// !$*UTF8*$!\n"+render({"archiveVersion":1,"classes":{},"objectVersion":56,"objects":objects,"rootObject":project})+"\n")
    info={"CFBundleDisplayName":"Jurvis","CFBundleName":"Jurvis","CFBundleIdentifier":"$(PRODUCT_BUNDLE_IDENTIFIER)","CFBundleExecutable":"$(EXECUTABLE_NAME)","CFBundlePackageType":"APPL","CFBundleShortVersionString":"$(MARKETING_VERSION)","CFBundleVersion":"$(CURRENT_PROJECT_VERSION)",
          "JurvisWorkspaceRoot":"$(JURVIS_WORKSPACE_ROOT)","LSApplicationCategoryType":"public.app-category.productivity","NSLocalNetworkUsageDescription":"连接你的 Mac，使用本地助理、生图和短剧工具。","NSCameraUsageDescription":"扫描 Mac 上的配对二维码。","NSAppTransportSecurity":{"NSAllowsLocalNetworking":True},
          "CFBundleURLTypes":[{"CFBundleURLName":"app.jurvis.pair","CFBundleURLSchemes":["jurvis"]}],"UILaunchScreen":{},"UISupportedInterfaceOrientations":["UIInterfaceOrientationPortrait","UIInterfaceOrientationLandscapeLeft","UIInterfaceOrientationLandscapeRight"],
          "UISupportedInterfaceOrientations~ipad":["UIInterfaceOrientationPortrait","UIInterfaceOrientationPortraitUpsideDown","UIInterfaceOrientationLandscapeLeft","UIInterfaceOrientationLandscapeRight"],"ITSAppUsesNonExemptEncryption":False,"NSHighResolutionCapable":True}
    (NATIVE/"Info.plist").write_bytes(plistlib.dumps(info))
    print(folder)

if __name__=="__main__":main()
